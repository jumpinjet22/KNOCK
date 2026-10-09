from knock.core.scene import SceneObservation


class MockVisionProvider:
    name = "mock-vision"

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return f"MOCK_DESCRIPTION:{len(image)}bytes"

    def observe(self, image: bytes) -> SceneObservation:
        return SceneObservation(summary=self.describe(image))
