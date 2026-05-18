# Host Status 节点生命周期状态 — 全栈实施记录

## 需求背景

为主机节点增加生命周期状态（`status`）字段，支持标记节点为"待下线"等状态，
以便在前端给用户明确的视觉提示，同时保留旧连接路径不被误删。

## 状态定义

| 状态 | 值 | 说明 |
|------|-----|------|
| 正常 | `active` | 默认状态，正常使用 |
| 待下线 | `deprecated` | 仍可连接，前端显示 ⚠️ 警告标识 + 半透明样式 |
| 已禁用 | `disabled` | 前端不显示 / 拒绝连接（预留，当前未实现过滤逻辑） |

## 数据流全链路

```
hosts.yaml (status 字段)
  → HostTreeYAMLSchema (Pydantic 解析)
    → _flatten_yaml_node() (传递 status)
      → HostCreate (携带 status)
        → create_host() / update_host() (写入 DB)
          → Host ORM (status 列)
            → HostResponse.from_orm_model() (序列化)
              → API JSON (status 字段)
                → 前端 Host 接口 (status 属性)
                  → HostList.tsx (视觉指示)
```

## 变更清单

### 后端 — `src/models/host.py`

- [x] 新增 `HostStatus` 枚举（`active` / `deprecated` / `disabled`）
- [x] `Host` ORM：新增 `status` 列（默认 `active`）
- [x] `HostTreeYAMLSchema`：新增 `status` 字段
- [x] `HostCreate`：新增 `status` 字段
- [x] `HostUpdate`：新增 `status` 可选字段
- [x] `HostResponse`：新增 `status` 字段 + `from_orm_model()` 传递

### 后端 — `src/services/host_manager.py`

- [x] 导入 `HostStatus`
- [x] `_UPDATABLE_FIELDS` 添加 `"status"`
- [x] `create_host()`：设置 `host.status = data.status`
- [x] `_flatten_yaml_node()`：根节点 / 嵌套节点均传递 `status=node.status`
- [x] `_host_needs_update()`：新增 `status` 比较
- [x] `_build_update_data()`：新增 `status` 字段

### 前端 — `frontend/src/services/api.ts`

- [x] 新增 `HostStatus` 类型别名
- [x] `Host` 接口添加 `status: HostStatus`
- [x] `CreateHostRequest` 接口添加 `status?: HostStatus`

### 前端 — `frontend/src/components/HostList.tsx`

- [x] 新增 `isDeprecated` 状态判断
- [x] deprecated 节点整行降低透明度（`opacity-60`）
- [x] 节点名称后添加 ⚠ 待下线 徽章（amber 色系）

### 配置 — `config/hosts.yaml`（已在之前完成）

- [x] 头部注释增加 `status` 字段说明
- [x] 旧 m12 节点标记 `status: deprecated`

## 验证

- [x] Python 类型检查：无新增错误
- [x] TypeScript 编译：`npx tsc --noEmit` 通过（0 错误）

## 遗留 / 后续

- [ ] `disabled` 状态的前端过滤逻辑（当前不隐藏，可作为后续迭代）
- [ ] 数据库迁移脚本（当前使用 `create_all` 自动建表，SQLite 场景下删库重建即可；如需正式迁移可用 Alembic）
