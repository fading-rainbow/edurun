from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from budao.start_button import StartButtonTemplate
from budao.target import RUN_ACTIVITY, TARGET, TargetInspection


def setup():
    image = Image.new("RGB", (200, 200), "black")
    ImageDraw.Draw(image).rectangle((55, 105, 110, 115), fill="white")
    box = (50, 100, 150, 150)
    template = StartButtonTemplate(image.crop(box), (200, 200), box)
    frame = TargetInspection(
        package=TARGET,
        activity=RUN_ACTIVITY,
        version="4.3.0",
        state="metrics",
        run_control_state="not_started_or_countdown",
        elapsed_s=0,
        distance_m=0,
        completed_checkpoints=0,
        counter_label="已打卡",
        gps_label="GPS信号优",
    )
    xml = (
        f'<hierarchy><node package="{TARGET}" '
        f'resource-id="{TARGET}:id/tv_start_pause_continue_run" '
        'class="android.widget.ProgressBar" visible-to-user="true" '
        'enabled="true" bounds="[50,100][150,150]"/></hierarchy>'
    )
    return template, frame, image, xml


def test_start_button_match_is_local_and_requires_actual_pixel_pattern():
    template, frame, image, xml = setup()
    assert template.matches(frame, image, xml)
    assert not template.matches(frame, Image.new("RGB", (200, 200)), xml)
    altered = image.copy()
    ImageDraw.Draw(altered).rectangle((55, 105, 110, 115), fill="red")
    assert not template.matches(frame, altered, xml)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_control_state": "running"},
        {"run_control_state": "paused"},
        {"elapsed_s": 1},
        {"completed_checkpoints": 1},
        {"distance_m": 10},
        {"issues": ("human_face_verification_required",)},
        {"activity": TARGET + ".CameraActivity"},
    ],
)
def test_runtime_pause_other_page_or_verification_never_matches_start(changes):
    template, frame, image, xml = setup()
    assert not template.matches(frame.model_copy(update=changes), image, xml)


def test_wrong_hidden_duplicate_or_malformed_button_unknown():
    template, frame, image, xml = setup()
    for changed in [
        "<broken",
        xml.replace("[50,100]", "[49,100]"),
        xml.replace('enabled="true"', 'enabled="false"'),
        xml.replace('visible-to-user="true"', 'visible-to-user="false"'),
        xml.replace("</hierarchy>", xml + "</hierarchy>"),
    ]:
        assert not template.matches(frame, image, changed)
    assert not template.matches(frame, image.resize((400, 400)), xml)


def test_private_template_no_flat_colour_reference_or_external_path(tmp_path):
    with pytest.raises(ValueError):
        StartButtonTemplate(Image.new("RGB", (100, 50), "green"), (200, 200), (50, 100, 150, 150))
    with pytest.raises(ValueError):
        StartButtonTemplate.from_private_file(
            tmp_path, Path("outside.png"), (200, 200), (50, 100, 150, 150)
        )
