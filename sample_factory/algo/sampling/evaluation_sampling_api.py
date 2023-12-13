from __future__ import annotations

import time
from collections import OrderedDict
from queue import Empty, Full, Queue
from threading import Thread
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np
from signal_slot.signal_slot import EventLoop, EventLoopObject, EventLoopStatus, signal
from torch import Tensor

from sample_factory.algo.learning.learner import Learner
from sample_factory.algo.runners.runner import MsgHandler, PolicyMsgHandler, Runner
from sample_factory.algo.sampling.sampler import AbstractSampler, ParallelSampler, SerialSampler
from sample_factory.algo.utils.env_info import EnvInfo
from sample_factory.algo.utils.misc import EPISODIC, SAMPLES_COLLECTED, STATS_KEY, TIMING_STATS, ExperimentStatus
from sample_factory.algo.utils.model_sharing import ParameterServer
from sample_factory.algo.utils.shared_buffers import BufferMgr
from sample_factory.algo.utils.tensor_dict import TensorDict, clone_tensordict
from sample_factory.cfg.configurable import Configurable
from sample_factory.utils.dicts import iterate_recursively
from sample_factory.utils.gpu_utils import set_global_cuda_envvars
from sample_factory.utils.typing import Config, InitModelData, PolicyID, StatusCode
from sample_factory.utils.utils import log


class SamplingLoop(EventLoopObject, Configurable):
    def __init__(self, cfg: Config, env_info: EnvInfo):
        Configurable.__init__(self, cfg)
        unique_name = SamplingLoop.__name__
        self.event_loop: EventLoop = EventLoop(unique_loop_name=f"{unique_name}_EvtLoop", serial_mode=cfg.serial_mode)
        self.event_loop.owner = self
        EventLoopObject.__init__(self, self.event_loop, object_id=unique_name)
        # self.event_loop.verbose = True

        # calculate how many episodes for each environment should be taken into account
        # we only want to use first N episodes (we don't want to bias ourselves with short episodes)
        total_envs = self.cfg.num_workers * self.cfg.num_envs_per_worker
        self.max_episode_number = self.cfg.sample_env_episodes / total_envs

        self.env_info = env_info
        self.iteration: int = 0

        self.buffer_mgr: Optional[BufferMgr] = None
        self.param_servers: Optional[Dict[PolicyID, ParameterServer]] = None

        self.new_trajectory_callback: Optional[Callable] = None
        self.status: Optional[StatusCode] = None

        self.ready: bool = False
        self.stopped: bool = False

        # samples_collected counts the total number of observations processed by the algorithm
        self.samples_collected = [0 for _ in range(self.cfg.num_policies)]

        self.stats = dict()  # regular (non-averaged) stats
        self.avg_stats = dict()

        self.policy_avg_stats: Dict[str, List[List]] = dict()

        # global msg handlers for messages from algo components
        self.msg_handlers: Dict[str, List[MsgHandler]] = {
            TIMING_STATS: [Runner._timing_msg_handler],
            STATS_KEY: [Runner._stats_msg_handler],
        }

        # handlers for policy-specific messages
        self.policy_msg_handlers: Dict[str, List[PolicyMsgHandler]] = {
            EPISODIC: [self._episodic_stats_handler],
            SAMPLES_COLLECTED: [Runner._samples_stats_handler],
        }

    @signal
    def model_initialized(self):
        ...

    @signal
    def trajectory_buffers_available(self):
        ...

    @signal
    def stop(self):
        ...

    def init(
        self, buffer_mgr: Optional[BufferMgr] = None, param_servers: Optional[Dict[PolicyID, ParameterServer]] = None
    ):
        set_global_cuda_envvars(self.cfg)

        self.buffer_mgr = buffer_mgr
        if self.buffer_mgr is None:
            self.buffer_mgr = BufferMgr(self.cfg, self.env_info)

        self.param_servers = param_servers
        if self.param_servers is None:
            self.param_servers = dict()
            for policy_id in range(self.cfg.num_policies):
                self.param_servers[policy_id] = ParameterServer(
                    policy_id, self.buffer_mgr.policy_versions, self.cfg.serial_mode
                )

        sampler_cls = SerialSampler if self.cfg.serial_mode else ParallelSampler
        sampler: AbstractSampler = sampler_cls(
            self.event_loop, self.buffer_mgr, self.param_servers, self.cfg, self.env_info
        )
        self.event_loop.start.connect(sampler.init)
        sampler.started.connect(self.on_sampler_started)
        sampler.initialized.connect(self.on_sampler_initialized)

        for policy_id in range(self.cfg.num_policies):
            sampler.connect_model_initialized(policy_id, self.model_initialized)
            sampler.connect_on_new_trajectories(policy_id, self.on_new_trajectories)
        sampler.connect_trajectory_buffers_available(self.trajectory_buffers_available)
        sampler.connect_report_msg(self._process_msg)

        for stoppable in sampler.stoppable_components():
            self.stop.connect(stoppable.on_stop)

    def _process_msg(self, msgs):
        if isinstance(msgs, (dict, OrderedDict)):
            msgs = (msgs,)

        if not (isinstance(msgs, (List, Tuple)) and isinstance(msgs[0], (dict, OrderedDict))):
            log.error("While parsing a message: expected a dictionary or list/tuple of dictionaries, found %r", msgs)
            return

        for msg in msgs:
            # some messages are policy-specific
            policy_id = msg.get("policy_id", None)

            for key in msg:
                for handler in self.msg_handlers.get(key, ()):
                    handler(self, msg)
                if policy_id is not None:
                    for handler in self.policy_msg_handlers.get(key, ()):
                        handler(self, msg, policy_id)

    @staticmethod
    def _episodic_stats_handler(runner: Runner, msg: Dict, policy_id: PolicyID) -> None:
        # the only difference between this function and the one from `Runner`
        # is that we store all stats in a list and not in the deque
        s = msg[EPISODIC]

        # skip invalid stats, potentially be not setting episode_number one could always add stats
        episode_number = s["episode_extra_stats"].get("episode_number", 0)
        if episode_number < runner.max_episode_number:
            for _, key, value in iterate_recursively(s):
                if key not in runner.policy_avg_stats:
                    runner.policy_avg_stats[key] = [[] for _ in range(runner.cfg.num_policies)]

                if isinstance(value, np.ndarray) and value.ndim > 0:
                    # if len(value) > runner.policy_avg_stats[key][policy_id].maxlen:
                    #     # increase maxlen to make sure we never ignore any stats from the environments
                    #     runner.policy_avg_stats[key][policy_id] = deque(maxlen=len(value))

                    runner.policy_avg_stats[key][policy_id].extend(value)
                else:
                    runner.policy_avg_stats[key][policy_id].append(value)

    def wait_until_ready(self):
        while not self.ready:
            log.debug(f"{self.object_id}: waiting for sampler to be ready...")
            time.sleep(0.5)

    def start(self, init_model_data: Optional[Dict[PolicyID, InitModelData]] = None):
        """Model initialization should kickstart the sampling loop."""
        for policy_id in range(self.cfg.num_policies):
            if init_model_data is None:
                self.model_initialized.emit(None)
            else:
                self.model_initialized.emit(init_model_data[policy_id])

    def set_new_trajectory_callback(self, cb: Callable) -> None:
        self.new_trajectory_callback = cb

    def on_sampler_started(self):
        self.ready = True

    def on_sampler_initialized(self):
        log.debug(f"{self.object_id}: sampler fully initialized!")

    def on_new_trajectories(self, trajectory_dicts: Iterable[Dict], device: str):
        for trajectory_dict in trajectory_dicts:
            traj_buffer_idx: int | slice = trajectory_dict["traj_buffer_idx"]
            if isinstance(traj_buffer_idx, slice):
                trajectory_slice = traj_buffer_idx
            else:
                trajectory_slice = slice(traj_buffer_idx, traj_buffer_idx + 1)

            # data for this trajectory is now available in the buffer
            # always use a slice so that returned tensors are the same dimensionality regardless of whether we
            # use batched or non-batched sampling
            traj = self.buffer_mgr.traj_tensors_torch[device][trajectory_slice]
            self.new_trajectory_callback(traj, [traj_buffer_idx], device)

    def yield_trajectory_buffers(self, available_buffers: Iterable[int | slice], device: str):
        # make this trajectory buffer available again
        self.buffer_mgr.traj_buffer_queues[device].put_many(available_buffers)
        self.iteration += 1
        for policy_id in range(self.cfg.num_policies):
            self.trajectory_buffers_available.emit(policy_id, self.iteration)

    def run(self) -> StatusCode:
        log.debug("Before event loop...")

        # noinspection PyBroadException
        try:
            evt_loop_status = self.event_loop.exec()
            self.status = (
                ExperimentStatus.INTERRUPTED
                if evt_loop_status == EventLoopStatus.INTERRUPTED
                else ExperimentStatus.SUCCESS
            )
            self.stop.emit()
        except Exception:
            log.exception(f"Uncaught exception in {self.object_id} evt loop")
            self.status = ExperimentStatus.FAILURE

        log.debug(f"{SamplingLoop.__name__} finished with {self.status=}")
        return self.status

    def stop_sampling(self):
        self.stop.emit()
        self.event_loop.stop()
        self.stopped = True


class EvalSamplingAPI:
    def __init__(
        self,
        cfg: Config,
        env_info: EnvInfo,
    ):
        self.cfg = cfg
        self.env_info = env_info

        self.buffer_mgr = None
        self.policy_versions_tensor = None
        self.param_servers: Dict[PolicyID, ParameterServer] = None
        self.init_model_data: Dict[PolicyID, InitModelData] = None
        self.learners: Dict[PolicyID, Learner] = None

        self.sampling_loop: SamplingLoop = None
        self.traj_queue: Queue = Queue(maxsize=100)

    def init(self):
        set_global_cuda_envvars(self.cfg)

        self.buffer_mgr = BufferMgr(self.cfg, self.env_info)
        self.policy_versions_tensor: Tensor = self.buffer_mgr.policy_versions

        self.param_servers = {}
        self.init_model_data = {}
        self.learners = {}
        for policy_id in range(self.cfg.num_policies):
            self.param_servers[policy_id] = ParameterServer(
                policy_id, self.policy_versions_tensor, self.cfg.serial_mode
            )
            self.learners[policy_id] = Learner(
                self.cfg, self.env_info, self.policy_versions_tensor, policy_id, self.param_servers[policy_id]
            )
            self.init_model_data[policy_id] = self.learners[policy_id].init()

        self.sampling_loop: SamplingLoop = SamplingLoop(self.cfg, self.env_info)
        self.sampling_loop.init(self.buffer_mgr, self.param_servers)
        self.sampling_loop.set_new_trajectory_callback(self._on_new_trajectories)
        self.sampling_thread: Thread = Thread(target=self.sampling_loop.run)
        self.sampling_thread.start()

        self.sampling_loop.wait_until_ready()

    @property
    def eval_stats(self):
        # it's possible that we would like to return additional stats, like fps or sth
        # those could be added here
        return self.sampling_loop.policy_avg_stats

    @property
    def eval_episodes_sampled(self):
        # TODO: for now we only look at the first policy,
        # maybe even in MARL we will look only at first policy?
        policy_id = 0
        return len(self.eval_stats.get("reward", [[] for _ in range(self.cfg.num_policies)])[policy_id])

    def start(self, init_model_data: Optional[Dict[PolicyID, InitModelData]] = None):
        if init_model_data is None:
            init_model_data = self.init_model_data
        self.sampling_loop.start(init_model_data)

    def _on_new_trajectories(self, traj: TensorDict, traj_buffer_indices: Iterable[int | slice], device: str):
        traj_clone = clone_tensordict(traj)  # we copied the data so we can release the buffer

        # just release buffers after every trajectory
        # we could alternatively have more sophisticated logic here, see i.e. batcher.py
        self.sampling_loop.yield_trajectory_buffers(traj_buffer_indices, device)

        while not self.sampling_loop.stopped:
            try:
                self.traj_queue.put(traj_clone, timeout=1.0, block=True)
                break
            except Full:
                log.debug(f"{self._on_new_trajectories.__name__}: trajectory queue full, waiting...")
                self.sampling_loop.event_loop.process_events()

    def get_trajectories_sync(self) -> Optional[TensorDict]:
        while not self.sampling_loop.stopped:
            try:
                traj = self.traj_queue.get(timeout=5.0)
                return traj
            except Empty:
                log.debug(f"{self.get_trajectories_sync.__name__}(): waiting for trajectories...")
                continue

        return None

    def stop(self) -> StatusCode:
        self.sampling_loop.stop_sampling()
        self.sampling_thread.join()
        return self.sampling_loop.status
