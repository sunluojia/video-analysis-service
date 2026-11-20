from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from video_analysis.core.web.dependencies import DatabaseDep
from video_analysis.modules.ai.config.models import AiModel, AiProvider, AiRoute
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.ai.config.schemas import (
    AiKind,
    ModelIn,
    ModelView,
    ProviderIn,
    ProviderView,
    RouteIn,
    RouteView,
)
from video_analysis.modules.ai.config.service import AiConfigService
from video_analysis.modules.scheduling.task.models import TaskKind

router = APIRouter(prefix="/ai", tags=["AI 配置"])


def _assign(entity: object, body: BaseModel) -> None:
    for name, value in body:
        setattr(entity, name, value)


@router.get("/providers")
async def list_providers(db: DatabaseDep) -> list[ProviderView]:
    async with db.transaction() as session:
        providers = await AiConfigRepository(session).list_all(AiProvider)
    return [ProviderView.model_validate(provider) for provider in providers]


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderIn, db: DatabaseDep) -> ProviderView:
    provider = AiProvider(**body.model_dump())
    async with db.transaction() as session:
        AiConfigRepository(session).add(provider)
    return ProviderView.model_validate(provider)


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: UUID, body: ProviderIn, db: DatabaseDep) -> ProviderView:
    async with db.transaction() as session:
        provider = await AiConfigRepository(session).require(AiProvider, provider_id, lock=True)
        _assign(provider, body)
    return ProviderView.model_validate(provider)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: UUID, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        config = AiConfigRepository(session)
        await config.delete(await config.require(AiProvider, provider_id))


@router.get("/models")
async def list_models(db: DatabaseDep) -> list[ModelView]:
    async with db.transaction() as session:
        models = await AiConfigRepository(session).list_all(AiModel)
    return [ModelView.model_validate(model) for model in models]


@router.post("/models", status_code=201)
async def create_model(body: ModelIn, db: DatabaseDep) -> ModelView:
    model = AiModel(**body.model_dump())
    async with db.transaction() as session:
        AiConfigRepository(session).add(model)
    return ModelView.model_validate(model)


@router.put("/models/{model_id}")
async def update_model(model_id: UUID, body: ModelIn, db: DatabaseDep) -> ModelView:
    async with db.transaction() as session:
        model = await AiConfigRepository(session).require(AiModel, model_id, lock=True)
        _assign(model, body)
    return ModelView.model_validate(model)


@router.delete("/models/{model_id}", status_code=204)
async def delete_model(model_id: UUID, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        config = AiConfigRepository(session)
        await config.delete(await config.require(AiModel, model_id))


@router.get("/routes")
async def list_routes(db: DatabaseDep) -> list[RouteView]:
    async with db.transaction() as session:
        routes = await AiConfigRepository(session).list_all(AiRoute)
    return [RouteView.model_validate(route) for route in routes]


@router.put("/routes/{kind}")
async def save_route(kind: AiKind, body: RouteIn, db: DatabaseDep) -> RouteView:
    async with db.transaction() as session:
        route = await AiConfigService(session).save_route(TaskKind(kind), body.model_id, body.share)
    return RouteView.model_validate(route)


@router.delete("/routes/{kind}", status_code=204)
async def delete_route(kind: AiKind, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        config = AiConfigRepository(session)
        await config.delete(await config.require(AiRoute, TaskKind(kind)))
