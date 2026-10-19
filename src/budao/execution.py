"""本机原生入口的单操作执行槽；重复开始不排队，停止不等同于提交。"""

from __future__ import annotations

import copy
import math
import secrets
import threading

from .finish_connector import FinishEvidence
from .session_runner import SessionRunner

MESSAGES = {
    "preparing": "正在提前核验有界输入；尚未开始",
    "starting": "原App新开始已确认，正在连接有界输入；尚未提交",
    "running": "正在观察本次原App记录；尚未提交",
    "finish_ready": "运行阈值已确认；未结束、未保存，仍需成绩核验",
    "verification_required": "原App要求本人验证；自动输入已停止",
    "paused": "自动输入已停止；保留未提交的原App记录",
    "error": "单次执行未完成；不重复开始，不回显私有异常",
    "finishing": "正在按授权正常结束保存；有效成绩仍待核验",
    "accepted": "本次新记录与原App有效次数增量已核对",
    "submitted_unconfirmed": "已正常保存；有效成绩尚未确认，不重复提交",
    "save_unconfirmed": "结束或保存尚未确认；请核对原App，不重试",
    "recovering": "界面或打卡地图恢复中；自动输入已停止，正在重新核对",
}


class ExecutionCoordinator:
    def __init__(self, preparation, runner_factory=None):
        if runner_factory is not None and not callable(runner_factory):
            raise ValueError("执行组件必须由本机明确配置，不能来自HTTP请求")
        self.preparation, self.runner_factory = preparation, runner_factory
        self.slot = threading.Lock()
        self.state_lock = threading.Lock()
        self.cancel = threading.Event()
        self.report = {"state": "unchecked", "message": "尚未检查准备条件", "checks": []}

    def status(self):
        with self.state_lock:
            return copy.deepcopy(self.report)

    def _set(self, report):
        with self.state_lock:
            self.report = copy.deepcopy(report)

    def begin(self, *, start: bool, photo_ready: bool):
        if type(start) is not bool or type(photo_ready) is not bool:
            raise ValueError("操作与照片就绪只接受布尔值")
        with self.state_lock:
            if not self.slot.acquire(blocking=False):
                return copy.deepcopy(self.report)  # 不排队，不生成另一个开始动作。
            self.cancel = threading.Event()
            self.report = {
                "state": "checking",
                "message": "正在核对当前客户端，请等待本次结果",
                "checks": [],
                "ready": False,
                "run_started": False,
                "records_submitted": 0,
                "operation_id": secrets.token_hex(16),
            }
        task = threading.Thread(target=self._work, args=(start, photo_ready), daemon=True)
        try:
            task.start()
        except Exception:
            self._set(self.status() | {"state": "error", "message": MESSAGES["error"]})
            self.slot.release()
            raise
        return self.status()

    @staticmethod
    def _eligible_preparation(report):
        checks = report.get("checks")
        if not isinstance(checks, list) or len(checks) != 6:
            return False
        keys = [item.get("key") for item in checks if isinstance(item, dict)]
        return (
            report.get("ready") is True
            and report.get("run_started") is False
            and type(report.get("records_submitted")) is int
            and report["records_submitted"] == 0
            and set(keys) == {"device", "login", "photo", "route", "motion", "runtime"}
            and len(set(keys)) == 6
            and all(item.get("state") == "pass" for item in checks)
        )

    def _publish_runner(self, value):
        # 不接受执行组件的任意额外字段/文案；坐标、卡片名、路径、照片和异常均不发给UI。
        state = value.get("state") if isinstance(value, dict) else None
        if state not in MESSAGES:
            raise ValueError("执行阶段未知")
        public = {
            "state": state,
            "message": MESSAGES[state],
            "run_started": value.get("run_started") is True,
            "records_submitted": 0,
            "record_valid_proven": False,
            "one_click_workflow_verified": False,
        }
        if state == "error":
            public["ready"] = False
        if (
            type(value.get("run_started")) is not bool
            or type(value.get("records_submitted")) is not int
        ):
            raise ValueError("运行状态结构未知")
        if not public["run_started"]:
            if state == "preparing" and value.get("preparation_phase") == "gps_readiness":
                public["message"] = "正在等待原App定位就绪与本任务同位置实收；尚未点击开始"
            elif (
                state == "error"
                and value.get("preparation_phase") == "gps_timeout"
                and value.get("start_action_attempted") is False
            ):
                public["message"] = "定位就绪等待超时；未点击开始，未提交记录"
            elif (
                state == "error"
                and value.get("preparation_phase") == "record_recovery"
                and value.get("start_action_attempted") is False
            ):
                public["message"] = "原App有恢复记录提示，需本人处理；未开始新的乐跑"
        elif (
            state == "error"
            and value.get("preparation_phase") == "post_start_gps_timeout"
            and value.get("start_action_attempted") is True
        ):
            public["message"] = "新开始已确认，定位接续超时；输入已停止并保留记录"
        proof = value.get("finish_evidence")
        if state in {"accepted", "submitted_unconfirmed"}:
            if (
                not isinstance(proof, FinishEvidence)
                or value["records_submitted"] != 1
                or value.get("record_valid_proven") is not proof.accepted
                or (state == "accepted") is not proof.accepted
            ):
                raise ValueError("提交或有效成绩缺少新记录/基线增量证据")
            public.update(records_submitted=1, record_valid_proven=proof.accepted)
        elif value["records_submitted"] != 0 or value.get("record_valid_proven") is not False:
            raise ValueError("本执行槽不接受无凭据的提交或有效成绩声称")
        for key in (
            "mode_restored_normal",
            "cleanup_unconfirmed",
            "app_pause_confirmed",
            "app_pause_unconfirmed",
            "start_action_attempted",
        ):
            if key in value:
                if type(value[key]) is not bool:
                    raise ValueError("清理状态未知")
                public[key] = value[key]
        for key, maximum in (
            ("distance_m", 100_000),
            ("elapsed_s", 86_400),
            ("completed_checkpoints", 3),
        ):
            if key in value and value[key] is not None:
                if (
                    type(value[key]) not in (int, float)
                    or not math.isfinite(value[key])
                    or not 0 <= value[key] <= maximum
                    or (key != "distance_m" and type(value[key]) is not int)
                ):
                    raise ValueError("运行数字未知")
                public[key] = value[key]
        with self.state_lock:
            if self.cancel.is_set() and state in {
                "preparing",
                "starting",
                "running",
                "recovering",
            }:
                return  # 延迟进度不能覆盖已经发出的停止请求。
            self.report.update(public)

    def _work(self, start, photo_ready):
        runner = None
        try:
            if self.cancel.is_set():
                return
            report = self.preparation.check(photo_ready)
            if self.cancel.is_set():
                return
            visible = report | {"operation_id": self.status()["operation_id"]}
            if start and self._eligible_preparation(report) and self.runner_factory is not None:
                # 工厂组装期间仍是活动操作，不能泄露ready终态让原生客户端提前停止轮询。
                visible.update(state="preparing", message=MESSAGES["preparing"])
            self._set(visible)
            if report.get("state") == "checking":
                self._set(
                    self.status()
                    | {
                        "state": "blocked",
                        "ready": False,
                        "message": "其他准备操作未结束；不排队或开始",
                    }
                )
                return
            if not start:
                return
            if not self._eligible_preparation(report) or self.runner_factory is None:
                expired = any(
                    check.get("detail") == "过期记录，需用户处理"
                    for check in report.get("checks", [])
                )
                self._set(
                    self.status()
                    | {
                        "state": "blocked",
                        "ready": False,
                        "message": (
                            "过期记录，需用户处理"
                            if expired
                            else "准备或执行组件未就绪；未开始乐跑"
                        ),
                    }
                )
                return
            runner = self.runner_factory()
            if not isinstance(runner, SessionRunner):
                raise ValueError("不是本机绑定的受守卫执行组件")
            runner.run(lambda: not self.cancel.is_set(), self._publish_runner, self.cancel)
        except Exception:
            self._set(
                self.status()
                | {
                    "state": "error",
                    "message": MESSAGES["error"],
                    "ready": False,
                    "run_started": isinstance(runner, SessionRunner) and runner.started is True,
                    "records_submitted": 0,
                    "record_valid_proven": False,
                }
            )
        finally:
            if self.cancel.is_set() and self.status().get("state") not in {
                "paused",
                "error",
                "finish_ready",
                "verification_required",
                "accepted",
                "submitted_unconfirmed",
                "save_unconfirmed",
            }:
                self._set(self.status() | {"state": "paused", "message": MESSAGES["paused"]})
            self.slot.release()

    def stop(self):
        with self.state_lock:
            if self.slot.locked():
                self.cancel.set()
                self.report.update(state="stopping", message="已请求停止；等待输入与模式清理确认")
            return copy.deepcopy(self.report)
