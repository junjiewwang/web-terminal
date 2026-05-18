"""主机资产管理 REST API。"""

from __future__ import annotations

import tempfile
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_db
from src.models.host import HostCreate, HostResponse, HostUpdate
from src.services.host_manager import HostManager

router = APIRouter(prefix="/api/hosts", tags=["hosts"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


def _get_manager(session: DbSessionDep) -> HostManager:
    return HostManager(session)


HostManagerDep = Annotated[HostManager, Depends(_get_manager)]


# ── YAML 导入模式 ──────────────────────────────

class ImportMode(str, Enum):
    """YAML 导入策略"""
    OVERWRITE = "overwrite"   # 清空现有 → 全量导入
    MERGE = "merge"           # 保留现有 + 按 name 匹配更新/新增


# ── CRUD 端点 ──────────────────────────────────

@router.get("", response_model=list[HostResponse])
async def list_hosts(
    manager: HostManagerDep,
    request: Request,
    tag: str | None = None,
) -> list[HostResponse]:
    """获取递归主机树，支持按标签过滤。"""
    return await manager.list_host_responses(tag=tag)


# ── YAML 同步/导入/导出（固定路径，必须在 /{host_id} 之前注册） ──

@router.post("/sync", response_model=dict[str, object])
async def sync_hosts_from_yaml(
    manager: HostManagerDep,
) -> dict[str, object]:
    """从 config/hosts.yaml 同步主机到数据库。"""
    yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "hosts.yaml"
    result = await manager.sync_from_yaml(yaml_path)

    if result.errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "hosts.yaml 校验失败，同步已中止",
                "errors": result.errors,
            },
        )

    return {
        **result.to_dict(),
        "message": f"同步完成: 新增 {result.added}, 更新 {result.updated}, 删除 {result.deleted}",
    }


@router.post("/import", response_model=dict[str, object])
async def import_hosts_from_yaml(
    file: UploadFile,
    manager: HostManagerDep,
    mode: ImportMode = Query(default=ImportMode.MERGE, description="导入模式: merge(合并) / overwrite(覆盖)"),
) -> dict[str, object]:
    """上传 YAML 文件导入主机到数据库。

    - **merge**: 保留现有数据，按 name 匹配更新/新增
    - **overwrite**: 清空现有数据，全量导入
    """
    # 校验文件类型
    if file.filename and not file.filename.endswith((".yaml", ".yml")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持 .yaml / .yml 文件",
        )

    # 读取文件内容
    content = await file.read()
    if not content.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件内容为空",
        )

    # 写入临时文件供 sync_from_yaml 使用
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".yaml", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        # overwrite 模式：先清空所有主机
        if mode == ImportMode.OVERWRITE:
            await manager.delete_all_hosts()

        result = await manager.sync_from_yaml(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "YAML 校验失败，导入已中止",
                "errors": result.errors,
            },
        )

    return {
        **result.to_dict(),
        "mode": mode.value,
        "message": f"导入完成({mode.value}): 新增 {result.added}, 更新 {result.updated}, 删除 {result.deleted}",
    }


@router.get("/export")
async def export_hosts_as_yaml(
    manager: HostManagerDep,
) -> Response:
    """导出所有主机为 YAML 文件下载。"""
    hosts = await manager.list_host_responses()
    yaml_content = _hosts_to_yaml(hosts)
    return Response(
        content=yaml_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=hosts.yaml"},
    )


# ── YAML 编辑器接口（纯文本 YAML 读写） ───────

class YamlUpdateRequest(BaseModel):
    """YAML 文本更新请求体"""
    content: str = Field(..., min_length=1, description="YAML 文本内容")
    mode: ImportMode = Field(default=ImportMode.MERGE, description="导入模式")


@router.get("/yaml")
async def get_hosts_yaml(
    manager: HostManagerDep,
) -> Response:
    """获取当前主机配置的 YAML 纯文本（用于页面编辑器）。"""
    hosts = await manager.list_host_responses()
    yaml_content = _hosts_to_yaml(hosts)
    return Response(content=yaml_content, media_type="text/yaml")


@router.put("/yaml", response_model=dict[str, object])
async def update_hosts_from_yaml_text(
    body: YamlUpdateRequest,
    manager: HostManagerDep,
) -> dict[str, object]:
    """接收 YAML 文本内容，校验后更新主机数据。

    - 先进行 YAML 格式校验和字段验证
    - 通过后按 mode 执行导入（默认 merge）
    """
    content = body.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="YAML 内容为空",
        )

    # 先尝试解析 YAML，快速反馈语法错误
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "YAML 语法错误", "errors": [str(e)]},
        )

    if not isinstance(parsed, dict) or "hosts" not in parsed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "YAML 格式错误", "errors": ["缺少顶层 'hosts' 字段"]},
        )

    # 写入临时文件供 sync_from_yaml 使用
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        if body.mode == ImportMode.OVERWRITE:
            await manager.delete_all_hosts()

        result = await manager.sync_from_yaml(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "YAML 校验失败，更新已中止",
                "errors": result.errors,
            },
        )

    return {
        **result.to_dict(),
        "mode": body.mode.value,
        "message": f"YAML 更新完成({body.mode.value}): 新增 {result.added}, 更新 {result.updated}, 删除 {result.deleted}",
    }


# ── 单主机 CRUD（路径参数路由） ────────────────

@router.get("/{host_id}", response_model=HostResponse)
async def get_host(
    host_id: int,
    manager: HostManagerDep,
) -> HostResponse:
    host = await manager.get_host_by_id(host_id)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.post("", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(
    data: HostCreate,
    manager: HostManagerDep,
) -> HostResponse:
    existing = await manager.get_host_by_name(data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"主机名已存在: {data.name}",
        )
    host = await manager.create_host(data)
    return HostResponse.from_orm_model(host)


@router.put("/{host_id}", response_model=HostResponse)
async def update_host(
    host_id: int,
    data: HostUpdate,
    manager: HostManagerDep,
) -> HostResponse:
    host = await manager.update_host(host_id, data)
    if not host:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")
    return HostResponse.from_orm_model(host)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(
    host_id: int,
    manager: HostManagerDep,
) -> None:
    deleted = await manager.delete_host(host_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"主机不存在: {host_id}")


def _host_to_dict(host: HostResponse) -> dict[str, Any]:
    """将 HostResponse 转换为 YAML 友好的字典结构。"""
    node: dict[str, Any] = {"name": host.name}

    # 根节点必须有连接信息
    if host.host_type.value == "root":
        node["hostname"] = host.hostname
        node["port"] = host.port
        node["username"] = host.username
        node["auth_type"] = host.auth_type.value
        if host.private_key_path:
            node["private_key_path"] = host.private_key_path

    # 可选字段
    if host.description:
        node["description"] = host.description
    if host.tags:
        node["tags"] = host.tags
    if host.ready_pattern:
        node["ready_pattern"] = host.ready_pattern
    if host.credential_ref:
        node["credential_ref"] = host.credential_ref
    if host.status.value != "active":
        node["status"] = host.status.value

    # 入口动作（嵌套节点）
    if host.entry.type.value != "none":
        entry_dict: dict[str, Any] = {"type": host.entry.type.value}
        if host.entry.value:
            entry_dict["value"] = host.entry.value
        if host.entry.success_pattern:
            entry_dict["success_pattern"] = host.entry.success_pattern
        if host.entry.steps:
            entry_dict["steps"] = [
                {"wait": s.wait, "send": s.send, "timeout": s.timeout}
                for s in host.entry.steps
            ]
        node["entry"] = entry_dict

    # 递归子节点
    if host.children:
        node["children"] = [_host_to_dict(child) for child in host.children]

    return node


def _hosts_to_yaml(hosts: list[HostResponse]) -> str:
    """将主机树列表转换为 YAML 字符串。"""
    data: dict[str, Any] = {"hosts": [_host_to_dict(h) for h in hosts]}
    return yaml.dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    )
