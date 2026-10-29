"""Stable query failure categories without source text or database credentials."""


class ProjectionNotReady(ValueError):
    pass


class QueryDatabaseUnavailable(RuntimeError):
    pass
