from typing import Annotated

from fastapi import Depends, Request

from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


DatabaseDep = Annotated[Database, Depends(get_database)]
WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
