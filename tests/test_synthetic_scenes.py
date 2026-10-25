import random

from knock.core.scene import format_scene_for_prompt
from knock.core.synthetic_scenes import sample_scene


def test_sample_scene_is_deterministic_per_seed() -> None:
    first = [sample_scene(random.Random(7)) for _ in range(3)]
    second = [sample_scene(random.Random(7)) for _ in range(3)]
    assert [format_scene_for_prompt(s) for s in first] == [
        format_scene_for_prompt(s) for s in second
    ]


def test_sample_scene_covers_awkward_cases() -> None:
    rng = random.Random(1)
    scenes = [sample_scene(rng) for _ in range(400)]
    assert any(s is None for s in scenes)
    present = [s for s in scenes if s is not None and s.observation is not None]
    assert any(s.observation.people_count == 0 for s in present if s.observation)
    assert any(
        s.observation.visible_text and "ignore" in s.observation.visible_text.lower()
        for s in present
        if s.observation
    )
    for scene in present:
        assert format_scene_for_prompt(scene)
