"""Authenticated history navigation; no report paths or caller-supplied ownership."""

from fastapi import Depends

from memory.episode_contracts import (
    Capture,
    EventQuery,
    HistoryChange,
    HistoryQuery,
    SourcePage,
    Window,
)


def routes(app, control, identity):
    def service():
        return control.episodes

    @app.post("/history/capture")
    def capture(value: Capture, owner=Depends(identity), history=Depends(service)):
        return history.capture(owner, value)

    @app.post("/history/search")
    def search(value: HistoryQuery, owner=Depends(identity), history=Depends(service)):
        return history.search(owner, value)

    @app.post("/history/recent")
    def recent(value: HistoryQuery, owner=Depends(identity), history=Depends(service)):
        return history.search(owner, value, recent=True)

    @app.post("/history/events/search")
    def events(value: EventQuery, owner=Depends(identity), history=Depends(service)):
        return history.events(owner, value)

    @app.post("/history/events/window")
    def window(value: Window, owner=Depends(identity), history=Depends(service)):
        return history.window(owner, value)

    @app.post("/history/source")
    def source(value: SourcePage, owner=Depends(identity), history=Depends(service)):
        return history.source_page(owner, value)

    @app.post("/history/{episode_id}/state")
    def change(
        episode_id: str, value: HistoryChange, owner=Depends(identity), history=Depends(service)
    ):
        return history.change(owner, episode_id, value)
