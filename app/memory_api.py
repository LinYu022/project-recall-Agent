"""Uses the control API's authenticated owner, never an owner supplied in JSON."""

from fastapi import Depends

from memory.contracts import (
    CandidateInput,
    Change,
    Edit,
    Extract,
    ProjectInput,
    Review,
    Search,
    Settings,
    SourceInput,
)


def routes(app, control, identity):
    def memory_service():
        return control.memory

    @app.post("/memory/projects")
    def project(value: ProjectInput, owner=Depends(identity), service=Depends(memory_service)):
        return service.project(owner, value)

    @app.get("/memory/settings")
    def settings(owner=Depends(identity), service=Depends(memory_service)):
        return service.settings(owner)

    @app.post("/memory/settings")
    def set_settings(value: Settings, owner=Depends(identity), service=Depends(memory_service)):
        return service.settings(owner, value)

    @app.post("/memory/sources")
    def source(value: SourceInput, owner=Depends(identity), service=Depends(memory_service)):
        return service.source(owner, value)

    @app.post("/memory/candidates")
    def candidate(value: CandidateInput, owner=Depends(identity), service=Depends(memory_service)):
        return service.candidate(owner, value)

    @app.get("/memory/candidates")
    def candidates(owner=Depends(identity), service=Depends(memory_service)):
        return service.candidates(owner)

    @app.post("/memory/candidates/{candidate_id}/review")
    def review(
        candidate_id: str, value: Review, owner=Depends(identity), service=Depends(memory_service)
    ):
        return service.review(owner, candidate_id, value)

    @app.post("/memory/search")
    def search(value: Search, owner=Depends(identity), service=Depends(memory_service)):
        return service.search(owner, value)

    @app.get("/memory/items/{memory_id}")
    def item(memory_id: str, owner=Depends(identity), service=Depends(memory_service)):
        return service.item(owner, memory_id)

    @app.post("/memory/items/{memory_id}/change")
    def change(
        memory_id: str, value: Change, owner=Depends(identity), service=Depends(memory_service)
    ):
        return service.change(owner, memory_id, value)

    @app.post("/memory/items/{memory_id}/edit")
    def edit(memory_id: str, value: Edit, owner=Depends(identity), service=Depends(memory_service)):
        return service.edit(owner, memory_id, value)

    @app.get("/memory/items/{memory_id}/history")
    def history(memory_id: str, owner=Depends(identity), service=Depends(memory_service)):
        return service.history(owner, memory_id)

    @app.post("/memory/extractions")
    def extract(value: Extract, owner=Depends(identity), service=Depends(memory_service)):
        return service.enqueue(owner, value.source_id)

    @app.get("/memory/extractions/{job_id}")
    def extraction(job_id: str, owner=Depends(identity), service=Depends(memory_service)):
        return service.extraction(owner, job_id)
