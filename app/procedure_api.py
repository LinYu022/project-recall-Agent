"""Procedure lifecycle endpoints use the existing authenticated project owner."""

from fastapi import Depends

from memory.procedure_contracts import AbortValidation, Candidate, Transition, ValidateProcedure


def routes(app, control, identity):
    def service():
        return control.procedures

    @app.post("/procedures/candidates")
    def candidate(value: Candidate, owner=Depends(identity), procedures=Depends(service)):
        return procedures.candidate(owner, value)

    @app.get("/procedures/{version_id}")
    def version(version_id: str, owner=Depends(identity), procedures=Depends(service)):
        return procedures.get(owner, version_id)

    @app.post("/procedures/{version_id}/validate")
    def validation(
        version_id: str,
        value: ValidateProcedure,
        owner=Depends(identity),
        procedures=Depends(service),
    ):
        return procedures.validate(owner, version_id, value)

    @app.post("/procedures/{version_id}/state")
    def transition(
        version_id: str, value: Transition, owner=Depends(identity), procedures=Depends(service)
    ):
        return procedures.transition(owner, version_id, value)

    @app.post("/procedures/{version_id}/rollback")
    def rollback(
        version_id: str, value: Transition, owner=Depends(identity), procedures=Depends(service)
    ):
        return procedures.rollback(owner, version_id, value)

    @app.get("/procedure-validations/{validation_id}")
    def inspect_validation(
        validation_id: str, owner=Depends(identity), procedures=Depends(service)
    ):
        return procedures.validation(owner, validation_id)

    @app.post("/procedure-validations/{validation_id}/abort")
    def abort(
        validation_id: str,
        value: AbortValidation,
        owner=Depends(identity),
        procedures=Depends(service),
    ):
        return procedures.abort_validation(owner, validation_id, value)
