from fastapi import FastAPI

from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision

app = FastAPI(title="KNOCK API", version="0.1.0")
orchestrator = Orchestrator()


@app.post("/respond", response_model=ResponseDecision)
def respond(event: VisitorEvent) -> ResponseDecision:
    return orchestrator.respond(event)
