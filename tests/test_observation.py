from PIL import Image, ImageDraw

from budao.calibration import Anchor, Calibration
from budao.models import PixelPoint, VisualObservation
from budao.observation import MappedObserver, TrustedStatus, ViewportGuard
from budao.routes import RoutePlanner, distance
from budao.simulation import demo_config


def setup():
    image = Image.new("RGB", (240, 240), "white")
    draw = ImageDraw.Draw(image)
    boxes = ((10, 10, 30, 30), (210, 10, 230, 30), (10, 210, 30, 230))
    for box in boxes:
        draw.rectangle((box[0], box[1], box[0] + 9, box[3]), fill="black")
    planner = RoutePlanner(demo_config())

    def anchor(x, y):
        return Anchor(PixelPoint(x=x + 20, y=y + 20), planner.geo((x, y)))

    calibration = Calibration(
        planner, [anchor(0, 0), anchor(200, 0), anchor(0, 200)], [anchor(100, 100)], "fixed-view"
    )

    class FakeVision:
        calls = 0

        def observe(self, _png):
            self.calls += 1
            return VisualObservation(
                screen="running",
                confidence=1,
                checkpoint_number=1,
                checkpoint_pixel=PixelPoint(x=120, y=120),
                distance_m=9999,
                completed_checkpoints=3,
                record_valid=True,
            )

    vision = FakeVision()
    observer = MappedObserver(calibration, ViewportGuard(image, boxes), vision, (0, 0, 240, 240))
    return image, observer, vision, planner


def test_model_cannot_invent_completed_state():
    image, observer, _, planner = setup()
    status = TrustedStatus("com.lptiyu.tanke", 200, 0, True)
    result = observer.observe(image, status, 1)
    assert result.distance_m == 200 and result.completed_checkpoints == 0
    assert result.record_valid is False
    assert distance(result.checkpoint, planner.geo((100, 100))) < 0.001


def test_viewport_change_blocks_model_call():
    image, observer, vision, _ = setup()
    moved = Image.new("RGB", image.size, "white")
    result = observer.observe(moved, TrustedStatus("com.lptiyu.tanke", 200, 0, True), 1)
    assert result.screen == "unknown" and vision.calls == 0


def test_external_app_image_not_transmitted():
    image, observer, vision, _ = setup()
    result = observer.observe(image, TrustedStatus("ad.browser", 200, 0, True), 1)
    assert result.screen == "unknown" and vision.calls == 0
