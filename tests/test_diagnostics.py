import json

import pytest

from autoclip.diagnostics import classify_error, diagnose_batch


@pytest.mark.parametrize("message, expected", [
    ("剪辑计划未通过：预计时长 295 秒不在目标范围内", ["duration_long"]),
    ("预计时长 87 秒不在目标范围内", ["duration_short"]),
    ("成片时长149.0秒偏离目标190秒", ["duration_short"]),
    ("预计时长 140 秒不在目标范围内", ["duration_unresolved"]),
    ("配音与画面时长无法匹配：7.0 秒画面 / 12.0 秒配音", ["voice_alignment"]),
    ("本批次已有覆盖此故事的成片，保留已完成版本", ["already_completed"]),
    ("模型接口 HTTP 401：预计时长 87 秒", ["service_failure"]),
    ("成片音视频或总时长校验失败", ["media_failure"]),
    ("无法识别的新错误", ["unknown"]),
])
def test_classify_known_and_unknown_failures(message, expected):
    assert classify_error(message) == expected


def test_embedded_script_cannot_change_diagnosis():
    error = '预计时长 295 秒不在目标范围内\n上次输出：{"text":"HTTP 401 配音服务失败"}'
    assert classify_error(error) == ["duration_long"]


def test_multiple_validation_issues_are_kept():
    error = "必须按原片时间递增\n引用了不存在的字幕 ID\n预计时长 87 秒不在目标范围内"
    assert classify_error(error) == ["timeline_order", "evidence", "duration_short"]


def test_batch_uses_its_limits_and_does_not_modify_inputs(tmp_path):
    episode = tmp_path / "03-example"
    episode.mkdir()
    (episode / "settings.json").write_text(json.dumps({"min_seconds": 60, "max_seconds": 180}))
    (episode / "rejected.json").write_text(json.dumps([
        {"title": "短片", "error": "成片时长59.0秒偏离目标100秒"},
        {"title": "故障", "error": "配音服务失败"},
    ]))
    before = {p: p.read_bytes() for p in episode.iterdir()}
    report = diagnose_batch(tmp_path)
    assert report["candidates_with_records"] == 2
    assert report["issue_counts"] == {"duration_short": 1, "service_failure": 1}
    assert {p: p.read_bytes() for p in episode.iterdir()} == before
