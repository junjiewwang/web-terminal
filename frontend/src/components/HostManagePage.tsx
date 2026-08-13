/**
 * 主机管理页面
 *
 * 功能：
 * - 树形展示所有主机节点
 * - 新增 / 编辑 / 删除主机
 * - YAML 编辑器（在线编辑 + 校验 + 更新）
 * - YAML 导入（文件上传）/ 导出（文件下载）
 */

import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import type {
  Host,
  HostType,
  HostStatus,
  EntryType,
  LoginStep,
  CreateHostRequest,
  CredentialNameItem,
} from "../services/api";
import {
  createHost,
  updateHost,
  deleteHost,
  importHostsYaml,
  exportHostsYaml,
  fetchHostsYaml,
  updateHostsYaml,
  listCredentialNames,
} from "../services/api";
import ConfirmDialog from "./ConfirmDialog";

// ── 类型定义 ──────────────────────────────────

interface HostFormData {
  name: string;
  hostname: string;
  port: number;
  username: string;
  auth_type: "key" | "password";
  private_key_path: string;
  password: string;
  entry_password: string;
  credential_ref: string;
  description: string;
  tags: string;
  ready_pattern: string;
  host_type: HostType;
  parent_id: number | null;
  status: HostStatus;
  entry_type: EntryType;
  entry_value: string;
  entry_success_pattern: string;
  entry_steps: LoginStep[];
}

const EMPTY_FORM: HostFormData = {
  name: "",
  hostname: "",
  port: 22,
  username: "root",
  auth_type: "key",
  private_key_path: "~/.ssh/id_rsa",
  password: "",
  entry_password: "",
  credential_ref: "",
  description: "",
  tags: "",
  ready_pattern: "",
  host_type: "root",
  parent_id: null,
  status: "active",
  entry_type: "none",
  entry_value: "",
  entry_success_pattern: "",
  entry_steps: [],
};

// ── 主组件 ────────────────────────────────────

interface HostManagePageProps {
  hosts: Host[];
  onHostsChange: () => void;
  /** 外部传入：要立即编辑的主机（侧栏「编辑」入口跳转时用），编辑完清空 */
  editTargetId?: number | null;
  /** 编辑目标被消费后回调（父组件清空 editTargetId） */
  onEditTargetConsumed?: () => void;
}

export default function HostManagePage({
  hosts,
  onHostsChange,
  editTargetId,
  onEditTargetConsumed,
}: HostManagePageProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [editingHost, setEditingHost] = useState<Host | null>(null);
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [formMode, setFormMode] = useState<"create" | "edit">("create");
  const [parentForNew, setParentForNew] = useState<number | null>(null);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [yamlEditorOpen, setYamlEditorOpen] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [loading, setLoading] = useState(false);

  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Host | null>(null);

  const showToast = useCallback((type: "success" | "error", text: string) => {
    setToast({ type, text });
    setTimeout(() => setToast(null), 4000);
  }, []);

  // ── 操作处理 ──────────────────────────────

  const handleCreate = useCallback((parentId: number | null = null) => {
    setEditingHost(null);
    setParentForNew(parentId);
    setFormMode("create");
    setIsFormOpen(true);
  }, []);

  const handleEdit = useCallback((host: Host) => {
    setEditingHost(host);
    setFormMode("edit");
    setIsFormOpen(true);
  }, []);

  // 侧栏「编辑」入口跳转：editTargetId 传入后，自动定位并打开编辑抽屉
  useEffect(() => {
    if (editTargetId == null || hosts.length === 0) return;
    const find = (list: Host[]): Host | undefined => {
      for (const h of list) {
        if (h.id === editTargetId) return h;
        const child = find(h.children ?? []);
        if (child) return child;
      }
      return undefined;
    };
    const target = find(hosts);
    if (target) {
      handleEdit(target);
      onEditTargetConsumed?.();
    }
  }, [editTargetId, hosts, handleEdit, onEditTargetConsumed]);

  const handleDelete = useCallback((host: Host) => {
    setPendingDelete(host);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const host = pendingDelete;
    setPendingDelete(null);
    try {
      await deleteHost(host.id);
      showToast("success", `已删除「${host.name}」`);
      onHostsChange();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "删除失败");
    }
  }, [pendingDelete, onHostsChange, showToast]);

  const handleFormSubmit = useCallback(async (data: CreateHostRequest) => {
    setLoading(true);
    try {
      if (formMode === "create") {
        await createHost(data);
        showToast("success", `主机「${data.name}」创建成功`);
      } else if (editingHost) {
        await updateHost(editingHost.id, data);
        showToast("success", `主机「${data.name}」已更新`);
      }
      setIsFormOpen(false);
      onHostsChange();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "操作失败");
    } finally {
      setLoading(false);
    }
  }, [formMode, editingHost, onHostsChange, showToast]);

  const handleExport = useCallback(async () => {
    try {
      await exportHostsYaml();
      showToast("success", "YAML 导出成功");
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "导出失败");
    }
  }, [showToast]);

  // ── 搜索过滤 ──────────────────────────────

  const filteredHosts = useMemo(() => {
    if (!searchQuery.trim()) return hosts;
    return hosts.filter((h) => hostMatchesSearch(h, searchQuery));
  }, [hosts, searchQuery]);

  const totalCount = useMemo(() => countAll(hosts), [hosts]);

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏 */}
      <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
        <div className="flex items-center gap-4">
          <div>
            <h2 className="text-lg font-semibold text-white">主机管理</h2>
            <p className="mt-0.5 text-xs text-gray-500">{totalCount} 个节点</p>
          </div>
          {/* 搜索框（集成到顶栏） */}
          <div className="relative">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索主机..."
              className="w-56 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 pl-8 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
            />
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-gray-600">⌕</span>
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-600 transition-colors hover:text-gray-300"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* 批量操作 dropdown */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setDropdownOpen((p) => !p)}
              className="rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-white/20 hover:bg-white/8"
            >
              ⚙ 批量操作
            </button>
            {dropdownOpen && (
              <>
                <button
                  type="button"
                  className="fixed inset-0 z-30"
                  onClick={() => setDropdownOpen(false)}
                  aria-label="关闭菜单"
                />
                <div className="absolute right-0 top-full z-40 mt-1 w-44 overflow-hidden rounded-xl border border-white/10 bg-gray-950/95 py-1 shadow-2xl backdrop-blur-xl">
                  <button
                    type="button"
                    onClick={() => { setYamlEditorOpen(true); setDropdownOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-xs text-gray-300 transition-colors hover:bg-white/5"
                  >
                    <span className="text-sm">✏️</span> YAML 编辑器
                  </button>
                  <button
                    type="button"
                    onClick={() => { setImportModalOpen(true); setDropdownOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-xs text-gray-300 transition-colors hover:bg-white/5"
                  >
                    <span className="text-sm">📥</span> 导入 YAML
                  </button>
                  <button
                    type="button"
                    onClick={() => { handleExport(); setDropdownOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-xs text-gray-300 transition-colors hover:bg-white/5"
                  >
                    <span className="text-sm">📤</span> 导出 YAML
                  </button>
                </div>
              </>
            )}
          </div>
          <button
            onClick={() => handleCreate(null)}
            className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/20"
          >
            + 新增根节点
          </button>
        </div>
      </div>

      {/* 树形列表 */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {filteredHosts.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-500">
            <div className="mb-3 text-4xl">📋</div>
            <p className="text-sm">{searchQuery ? "无匹配结果" : "暂无主机，点击「新增根节点」开始"}</p>
          </div>
        ) : (
          <div className="space-y-1">
            {filteredHosts.map((host) => (
              <HostTreeNode
                key={host.id}
                host={host}
                depth={0}
                onEdit={handleEdit}
                onDelete={handleDelete}
                onCreate={handleCreate}
                searchQuery={searchQuery}
              />
            ))}
          </div>
        )}
      </div>

      {/* 编辑表单（侧边抽屉） */}
      {isFormOpen && (
        <HostEditDrawer
          mode={formMode}
          host={editingHost}
          parentId={parentForNew}
          hosts={hosts}
          onSubmit={handleFormSubmit}
          onClose={() => setIsFormOpen(false)}
          loading={loading}
        />
      )}

      {/* 导入弹窗 */}
      {importModalOpen && (
        <ImportModal
          onClose={() => setImportModalOpen(false)}
          onSuccess={() => {
            setImportModalOpen(false);
            onHostsChange();
          }}
          showToast={showToast}
        />
      )}

      {/* YAML 编辑器弹窗 */}
      {yamlEditorOpen && (
        <YamlEditorModal
          onClose={() => setYamlEditorOpen(false)}
          onSuccess={() => {
            setYamlEditorOpen(false);
            onHostsChange();
          }}
          showToast={showToast}
        />
      )}

      {/* 删除确认弹窗 */}
      {pendingDelete && (
        <ConfirmDialog
          open={!!pendingDelete}
          danger
          title="删除主机"
          message={
            countChildren(pendingDelete) > 0
              ? `确认删除「${pendingDelete.name}」及其 ${countChildren(pendingDelete)} 个子节点？此操作不可撤销。`
              : `确认删除「${pendingDelete.name}」？此操作不可撤销。`
          }
          confirmText="删除"
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}

      {/* Toast 通知 */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 z-50 rounded-xl border px-4 py-3 text-sm shadow-2xl backdrop-blur ${
            toast.type === "success"
              ? "border-emerald-500/30 bg-emerald-950/90 text-emerald-300"
              : "border-red-500/30 bg-red-950/90 text-red-300"
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  );
}

// ── 树形节点组件 ──────────────────────────────

interface HostTreeNodeProps {
  host: Host;
  depth: number;
  onEdit: (host: Host) => void;
  onDelete: (host: Host) => void;
  onCreate: (parentId: number | null) => void;
  searchQuery?: string;
}

function HostTreeNode({ host, depth, onEdit, onDelete, onCreate, searchQuery }: HostTreeNodeProps) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = host.children && host.children.length > 0;

  const statusColor = host.status === "active"
    ? "bg-emerald-400"
    : host.status === "deprecated"
    ? "bg-amber-400"
    : "bg-red-400";

  return (
    <div>
      <div
        className={`group relative flex items-center gap-3 rounded-xl border border-transparent px-3 py-2.5 transition-colors hover:border-white/8 hover:bg-white/[0.03] ${
          host.status === "disabled" ? "opacity-50" : ""
        }`}
        style={{ paddingLeft: `${depth * 24 + 12}px` }}
      >
        {/* 左侧状态指示条 */}
        <span className={`absolute left-1 top-2 bottom-2 w-0.5 rounded-full ${statusColor} ${
          host.status === "active" ? "opacity-40" : "opacity-70"
        }`} />

        {/* 展开/折叠 */}
        {hasChildren ? (
          <button
            onClick={() => setExpanded((p) => !p)}
            className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-[10px] text-gray-500 transition-transform hover:text-gray-300"
            style={{ transform: expanded ? "rotate(90deg)" : "rotate(0deg)" }}
          >
            ▶
          </button>
        ) : (
          <span className="flex h-5 w-5 shrink-0 items-center justify-center text-[10px] text-gray-700">•</span>
        )}

        {/* 图标 */}
        <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border text-sm ${
          host.host_type === "root"
            ? "border-blue-400/20 bg-blue-400/10"
            : "border-white/8 bg-white/5"
        }`}>
          {host.host_type === "root" ? "🖥" : "🔗"}
        </span>

        {/* 信息 */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-100">
              <Highlight text={host.name} query={searchQuery} />
            </span>
            {host.status === "deprecated" && (
              <span className="shrink-0 rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-400">
                待下线
              </span>
            )}
            {host.status === "disabled" && (
              <span className="shrink-0 rounded-full border border-red-500/30 bg-red-500/10 px-1.5 py-0.5 text-[10px] text-red-400">
                已禁用
              </span>
            )}
            {hasChildren && (
              <span className="shrink-0 rounded-full border border-white/8 bg-white/[0.03] px-1.5 py-0.5 text-[10px] text-gray-500">
                {host.children.length}
              </span>
            )}
            {/* 标签（总是显示，不仅搜索时） */}
            {host.tags.length > 0 && (
              <div className="hidden items-center gap-1 sm:flex">
                {host.tags.slice(0, 2).map((tag) => (
                  <span key={tag} className="rounded-full border border-white/6 bg-white/[0.03] px-1.5 py-0.5 text-[9px] text-gray-500">
                    {tag}
                  </span>
                ))}
                {host.tags.length > 2 && (
                  <span className="text-[9px] text-gray-600">+{host.tags.length - 2}</span>
                )}
              </div>
            )}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-gray-500">
            {host.host_type === "root"
              ? `${host.username}@${host.hostname}:${host.port}`
              : host.entry.type === "menu_send"
              ? `→ ${host.entry.value ?? ""}`
              : host.entry.type === "ssh_command"
              ? `$ ${host.entry.value ?? ""}`
              : host.name}
            {host.description && (
              <span className="ml-1 text-gray-600">· {host.description.split("\n")[0].slice(0, 40)}</span>
            )}
          </div>
        </div>

        {/* 操作按钮 */}
        <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
          <button
            onClick={() => onCreate(host.id)}
            className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-emerald-400/30 hover:text-emerald-300"
            title="添加子节点"
          >
            + 子节点
          </button>
          <button
            onClick={() => onEdit(host)}
            className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-blue-400/30 hover:text-blue-300"
            title="编辑"
          >
            编辑
          </button>
          <button
            onClick={() => onDelete(host)}
            className="rounded-md border border-white/8 bg-white/5 px-2 py-1 text-[10px] text-gray-400 transition-colors hover:border-red-400/30 hover:text-red-300"
            title="删除"
          >
            删除
          </button>
        </div>
      </div>

      {/* 子节点 */}
      {hasChildren && expanded && (
        <div>
          {host.children.map((child) => (
            <HostTreeNode
              key={child.id}
              host={child}
              depth={depth + 1}
              onEdit={onEdit}
              onDelete={onDelete}
              onCreate={onCreate}
              searchQuery={searchQuery}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── 编辑抽屉 ──────────────────────────────────

interface HostEditDrawerProps {
  mode: "create" | "edit";
  host: Host | null;
  parentId: number | null;
  hosts: Host[];
  onSubmit: (data: CreateHostRequest) => void;
  onClose: () => void;
  loading: boolean;
}

function HostEditDrawer({ mode, host, parentId, hosts: _hosts, onSubmit, onClose, loading }: HostEditDrawerProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const initial: HostFormData = host
    ? {
        name: host.name,
        hostname: host.hostname,
        port: host.port,
        username: host.username,
        auth_type: host.auth_type,
        private_key_path: host.private_key_path ?? "",
        password: "",
        entry_password: "",
        credential_ref: host.credential_ref ?? "",
        description: host.description ?? "",
        tags: host.tags.join(", "),
        ready_pattern: host.ready_pattern ?? "",
        host_type: host.host_type,
        parent_id: host.parent_id ?? null,
        status: host.status,
        entry_type: host.entry.type,
        entry_value: host.entry.value ?? "",
        entry_success_pattern: host.entry.success_pattern ?? "",
        entry_steps: host.entry.steps ?? [],
      }
    : {
        ...EMPTY_FORM,
        host_type: parentId ? "nested" : "root",
        parent_id: parentId,
      };

  const [form, setForm] = useState<HostFormData>(initial);

  const handleChange = useCallback((field: keyof HostFormData, value: unknown) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const data: CreateHostRequest = {
        name: form.name.trim(),
        hostname: form.hostname.trim() || "0.0.0.0",
        port: form.port,
        username: form.username.trim() || "root",
        auth_type: form.auth_type,
        host_type: form.host_type,
        status: form.status,
      };

      if (form.parent_id) data.parent_id = form.parent_id;
      if (form.private_key_path.trim()) data.private_key_path = form.private_key_path.trim();
      if (form.password.trim()) data.password = form.password.trim();
      if (form.entry_password.trim()) data.entry_password = form.entry_password.trim();
      if (form.credential_ref.trim()) data.credential_ref = form.credential_ref.trim();
      if (form.description.trim()) data.description = form.description.trim();
      if (form.tags.trim()) data.tags = form.tags.split(",").map((t) => t.trim()).filter(Boolean);
      if (form.ready_pattern.trim()) data.ready_pattern = form.ready_pattern.trim();

      if (form.entry_type !== "none") {
        data.entry = {
          type: form.entry_type,
          value: form.entry_value.trim() || null,
          success_pattern: form.entry_success_pattern.trim() || null,
          steps: form.entry_steps,
        };
      }

      onSubmit(data);
    },
    [form, onSubmit],
  );

  return (
    <>
      {/* Backdrop */}
      <button
        type="button"
        className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[2px]"
        onClick={onClose}
        aria-label="关闭"
      />

      {/* Drawer */}
      <aside className="fixed inset-y-0 right-0 z-50 w-[480px] max-w-[95vw] overflow-y-auto border-l border-white/10 bg-gray-950/95 shadow-[-24px_0_60px_rgba(0,0,0,0.4)] backdrop-blur-xl">
        <form onSubmit={handleSubmit} className="flex h-full flex-col">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
            <h3 className="text-base font-semibold text-white">
              {mode === "create" ? "新增主机" : `编辑 — ${host?.name}`}
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full p-1 text-gray-500 transition-colors hover:text-white"
            >
              ✕
            </button>
          </div>

          {/* Form Body */}
          <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
            {/* 基本信息 */}
            <FieldGroup title="基本信息">
              <Field label="名称 *" value={form.name} onChange={(v) => handleChange("name", v)} placeholder="如 dev-server" />
              <div className="grid grid-cols-2 gap-3">
                <SelectField
                  label="节点类型"
                  value={form.host_type}
                  options={[
                    { value: "root", label: "根节点" },
                    { value: "nested", label: "嵌套节点" },
                  ]}
                  onChange={(v) => handleChange("host_type", v)}
                />
                <SelectField
                  label="状态"
                  value={form.status}
                  options={[
                    { value: "active", label: "正常" },
                    { value: "deprecated", label: "待下线" },
                    { value: "disabled", label: "禁用" },
                  ]}
                  onChange={(v) => handleChange("status", v)}
                />
              </div>
              <Field label="描述" value={form.description} onChange={(v) => handleChange("description", v)} placeholder="可选备注" />
              <Field label="标签" value={form.tags} onChange={(v) => handleChange("tags", v)} placeholder="逗号分隔，如 dev,linux" />
            </FieldGroup>

            {/* 连接信息（根节点） */}
            {form.host_type === "root" && (
              <FieldGroup title="连接信息">
                <Field label="主机地址 *" value={form.hostname} onChange={(v) => handleChange("hostname", v)} placeholder="如 192.168.1.100" />
                <div className="grid grid-cols-2 gap-3">
                  <Field label="端口" value={String(form.port)} onChange={(v) => handleChange("port", parseInt(v) || 22)} type="number" />
                  <Field label="用户名" value={form.username} onChange={(v) => handleChange("username", v)} />
                </div>
                <SelectField
                  label="认证方式"
                  value={form.auth_type}
                  options={[
                    { value: "key", label: "SSH Key" },
                    { value: "password", label: "密码" },
                  ]}
                  onChange={(v) => handleChange("auth_type", v)}
                />
                {form.auth_type === "key" && (
                  <Field label="私钥路径" value={form.private_key_path} onChange={(v) => handleChange("private_key_path", v)} placeholder="~/.ssh/id_rsa" />
                )}
                {form.auth_type === "password" && (
                  <Field label="SSH 密码" value={form.password} onChange={(v) => handleChange("password", v)} type="password" placeholder="留空则不修改" />
                )}
              </FieldGroup>
            )}

            {/* 入口动作（嵌套节点） */}
            {form.host_type === "nested" && (
              <FieldGroup title="入口动作">
                <SelectField
                  label="入口类型"
                  value={form.entry_type}
                  options={[
                    { value: "none", label: "无" },
                    { value: "menu_send", label: "菜单发送" },
                    { value: "ssh_command", label: "SSH 命令" },
                  ]}
                  onChange={(v) => handleChange("entry_type", v)}
                />
                {form.entry_type !== "none" && (
                  <>
                    <Field
                      label="入口值"
                      value={form.entry_value}
                      onChange={(v) => handleChange("entry_value", v)}
                      placeholder={form.entry_type === "menu_send" ? "如 1（菜单选项）或 IP 地址" : "如 ssh user@host"}
                    />
                    <Field
                      label="成功匹配模式"
                      value={form.entry_success_pattern}
                      onChange={(v) => handleChange("entry_success_pattern", v)}
                      placeholder="正则表达式，如 [$#>%]\\s*$"
                    />
                    {/* 交互步骤编辑器 */}
                    <StepsEditor
                      steps={form.entry_steps}
                      onChange={(steps) => handleChange("entry_steps", steps)}
                    />
                  </>
                )}
                <Field label="入口密码" value={form.entry_password} onChange={(v) => handleChange("entry_password", v)} type="password" placeholder="用于 {{password}} 变量替换，留空则不设置" />
                <CredentialRefField value={form.credential_ref} onChange={(v) => handleChange("credential_ref", v)} />
              </FieldGroup>
            )}

            {/* 高级设置 */}
            <FieldGroup title="高级设置">
              <Field label="就绪匹配模式" value={form.ready_pattern} onChange={(v) => handleChange("ready_pattern", v)} placeholder="正则表达式，用于检测终端就绪" />
            </FieldGroup>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-3 border-t border-white/8 px-6 py-4">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-white/10"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={loading || !form.name.trim()}
              className="rounded-lg border border-emerald-500/30 bg-emerald-500/15 px-4 py-2 text-sm font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/25 disabled:opacity-50"
            >
              {loading ? "保存中..." : mode === "create" ? "创建" : "保存"}
            </button>
          </div>
        </form>
      </aside>
    </>
  );
}

// ── 导入弹窗 ──────────────────────────────────

interface ImportModalProps {
  onClose: () => void;
  onSuccess: () => void;
  showToast: (type: "success" | "error", text: string) => void;
}

function ImportModal({ onClose, onSuccess, showToast }: ImportModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"merge" | "overwrite">("merge");
  const [loading, setLoading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleImport = useCallback(async () => {
    if (!file) return;
    setLoading(true);
    try {
      const result = await importHostsYaml(file, mode);
      showToast("success", result.message);
      onSuccess();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "导入失败");
    } finally {
      setLoading(false);
    }
  }, [file, mode, onSuccess, showToast]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const dropped = e.dataTransfer.files[0];
    if (dropped && (dropped.name.endsWith(".yaml") || dropped.name.endsWith(".yml"))) {
      setFile(dropped);
    }
  }, []);

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭"
      />
      <div className="fixed left-1/2 top-1/2 z-50 w-[420px] max-w-[90vw] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-gray-950/95 p-6 shadow-2xl backdrop-blur-xl">
        <h3 className="mb-4 text-base font-semibold text-white">导入 YAML</h3>

        {/* 文件拖拽区 */}
        <div
          onDragOver={(e) => e.preventDefault()}
          onDrop={handleDrop}
          className="flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-white/10 bg-white/[0.02] px-4 py-8 transition-colors hover:border-emerald-400/30"
        >
          {file ? (
            <div className="text-center">
              <p className="text-sm text-gray-200">📄 {file.name}</p>
              <p className="mt-1 text-xs text-gray-500">{(file.size / 1024).toFixed(1)} KB</p>
              <button
                type="button"
                onClick={() => setFile(null)}
                className="mt-2 text-xs text-red-400 hover:text-red-300"
              >
                移除
              </button>
            </div>
          ) : (
            <div className="text-center">
              <p className="text-3xl">📁</p>
              <p className="mt-2 text-sm text-gray-400">拖拽 YAML 文件到此处</p>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="mt-2 text-xs text-emerald-400 hover:text-emerald-300"
              >
                或点击选择文件
              </button>
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".yaml,.yml"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>

        {/* 导入模式 */}
        <div className="mt-4">
          <p className="mb-2 text-xs font-medium text-gray-400">导入模式</p>
          <div className="flex gap-3">
            <label className="flex cursor-pointer items-center gap-2 text-sm text-gray-300">
              <input
                type="radio"
                name="import-mode"
                checked={mode === "merge"}
                onChange={() => setMode("merge")}
                className="accent-emerald-400"
              />
              合并（保留现有 + 新增/更新）
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-sm text-gray-300">
              <input
                type="radio"
                name="import-mode"
                checked={mode === "overwrite"}
                onChange={() => setMode("overwrite")}
                className="accent-emerald-400"
              />
              覆盖（清空后全量导入）
            </label>
          </div>
          {mode === "overwrite" && (
            <p className="mt-2 text-[11px] text-amber-400">⚠️ 覆盖模式将删除所有现有主机数据！</p>
          )}
        </div>

        {/* 操作按钮 */}
        <div className="mt-5 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm text-gray-300 hover:bg-white/10"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleImport}
            disabled={!file || loading}
            className="rounded-lg border border-emerald-500/30 bg-emerald-500/15 px-4 py-2 text-sm font-medium text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
          >
            {loading ? "导入中..." : "开始导入"}
          </button>
        </div>
      </div>
    </>
  );
}

// ── YAML 编辑器弹窗 ──────────────────────────────

interface YamlEditorModalProps {
  onClose: () => void;
  onSuccess: () => void;
  showToast: (type: "success" | "error", text: string) => void;
}

function YamlEditorModal({ onClose, onSuccess, showToast }: YamlEditorModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const [content, setContent] = useState("");
  const [mode, setMode] = useState<"merge" | "overwrite">("merge");
  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);

  // 加载当前 YAML 配置
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const yaml = await fetchHostsYaml();
        if (!cancelled) setContent(yaml);
      } catch (err) {
        if (!cancelled) {
          showToast("error", err instanceof Error ? err.message : "加载 YAML 失败");
        }
      } finally {
        if (!cancelled) setFetching(false);
      }
    })();
    return () => { cancelled = true; };
  }, [showToast]);

  const handleSubmit = useCallback(async () => {
    if (!content.trim()) {
      setErrors(["YAML 内容不能为空"]);
      return;
    }
    setErrors([]);
    setLoading(true);
    try {
      const result = await updateHostsYaml(content, mode);
      if (result.errors?.length) {
        setErrors(result.errors);
        showToast("error", `部分校验未通过（${result.errors.length} 个错误）`);
      } else {
        showToast("success", result.message || "YAML 更新成功");
        onSuccess();
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "更新失败";
      // 解析多行错误信息
      const lines = msg.split("\n");
      if (lines.length > 1) {
        setErrors(lines.slice(1));
        showToast("error", lines[0]);
      } else {
        setErrors([msg]);
        showToast("error", msg);
      }
    } finally {
      setLoading(false);
    }
  }, [content, mode, onSuccess, showToast]);

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="关闭"
      />
      <div className="fixed inset-4 z-50 flex flex-col rounded-2xl border border-white/10 bg-gray-950/95 shadow-2xl backdrop-blur-xl lg:inset-12">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-white/8 px-6 py-4">
          <div>
            <h3 className="text-base font-semibold text-white">YAML 编辑器</h3>
            <p className="mt-0.5 text-xs text-gray-500">在线编辑主机配置，支持语法校验</p>
          </div>
          <div className="flex items-center gap-3">
            {/* 导入模式选择 */}
            <div className="flex items-center gap-2 rounded-lg border border-white/8 bg-white/[0.03] px-3 py-1.5">
              <span className="text-[11px] text-gray-500">模式:</span>
              <label className="flex cursor-pointer items-center gap-1 text-xs text-gray-300">
                <input
                  type="radio"
                  name="yaml-mode"
                  checked={mode === "merge"}
                  onChange={() => setMode("merge")}
                  className="accent-emerald-400"
                />
                合并
              </label>
              <label className="flex cursor-pointer items-center gap-1 text-xs text-gray-300">
                <input
                  type="radio"
                  name="yaml-mode"
                  checked={mode === "overwrite"}
                  onChange={() => setMode("overwrite")}
                  className="accent-emerald-400"
                />
                覆盖
              </label>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full p-1.5 text-gray-500 transition-colors hover:bg-white/10 hover:text-white"
            >
              ✕
            </button>
          </div>
        </div>

        {/* 覆盖模式警告 */}
        {mode === "overwrite" && (
          <div className="border-b border-amber-500/20 bg-amber-500/5 px-6 py-2">
            <p className="text-[11px] text-amber-400">⚠️ 覆盖模式将删除所有现有主机数据并全量替换为编辑器内容！</p>
          </div>
        )}

        {/* Editor body */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {fetching ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="text-sm text-gray-500">加载配置中...</p>
            </div>
          ) : (
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
              className="flex-1 resize-none border-none bg-transparent px-6 py-4 font-mono text-sm leading-relaxed text-gray-200 placeholder:text-gray-600 focus:outline-none"
              placeholder="# hosts.yaml&#10;hosts:&#10;  - name: my-server&#10;    hostname: 192.168.1.100&#10;    port: 22&#10;    username: root"
            />
          )}

          {/* 校验错误展示 */}
          {errors.length > 0 && (
            <div className="border-t border-red-500/20 bg-red-950/30 px-6 py-3">
              <p className="mb-1.5 text-[11px] font-medium text-red-400">校验错误：</p>
              <ul className="max-h-24 space-y-0.5 overflow-y-auto">
                {errors.map((err, idx) => (
                  <li key={idx} className="font-mono text-[11px] text-red-300/80">
                    • {err}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-white/8 px-6 py-4">
          <p className="text-[11px] text-gray-600">
            {content ? `${content.split("\n").length} 行` : "空"}
          </p>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-white/10"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={loading || fetching || !content.trim()}
              className="rounded-lg border border-emerald-500/30 bg-emerald-500/15 px-4 py-2 text-sm font-medium text-emerald-300 transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/25 disabled:opacity-50"
            >
              {loading ? "校验并保存中..." : "校验并保存"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

// ── 表单组件 ──────────────────────────────────

function FieldGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-500">{title}</h4>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <div>
      <label className="mb-1 block text-[11px] text-gray-500">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
      />
    </div>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="mb-1 block text-[11px] text-gray-500">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 focus:border-emerald-500/40 focus:outline-none"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function Highlight({ text, query }: { text: string; query?: string }) {
  if (!query?.trim()) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <span className="font-semibold text-emerald-400">{text.slice(idx, idx + query.length)}</span>
      {text.slice(idx + query.length)}
    </>
  );
}

// ── 交互步骤编辑器 ──────────────────────────────

interface StepsEditorProps {
  steps: LoginStep[];
  onChange: (steps: LoginStep[]) => void;
}

function StepsEditor({ steps, onChange }: StepsEditorProps) {
  const addStep = useCallback(() => {
    onChange([...steps, { wait: "", send: "", timeout: 10 }]);
  }, [steps, onChange]);

  const removeStep = useCallback((idx: number) => {
    onChange(steps.filter((_, i) => i !== idx));
  }, [steps, onChange]);

  const updateStep = useCallback((idx: number, field: keyof LoginStep, value: string | number) => {
    onChange(steps.map((step, i) => (i === idx ? { ...step, [field]: value } : step)));
  }, [steps, onChange]);

  const moveStep = useCallback((idx: number, direction: -1 | 1) => {
    const targetIdx = idx + direction;
    if (targetIdx < 0 || targetIdx >= steps.length) return;
    const newSteps = [...steps];
    [newSteps[idx], newSteps[targetIdx]] = [newSteps[targetIdx], newSteps[idx]];
    onChange(newSteps);
  }, [steps, onChange]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <label className="text-[11px] text-gray-500">交互步骤（按顺序执行）</label>
        <button
          type="button"
          onClick={addStep}
          className="rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20"
        >
          + 添加步骤
        </button>
      </div>

      {steps.length === 0 ? (
        <div className="rounded-lg border border-dashed border-white/10 bg-white/[0.02] px-3 py-4 text-center">
          <p className="text-[11px] text-gray-600">暂无交互步骤</p>
          <p className="mt-1 text-[10px] text-gray-700">
            适用于需要依次等待提示并发送响应的场景（如密码输入、菜单选择）
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {steps.map((step, idx) => (
            <div
              key={idx}
              className="rounded-lg border border-white/8 bg-white/[0.02] p-3"
            >
              {/* 步骤头部：序号 + 操作 */}
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[10px] font-medium text-gray-500">
                  步骤 {idx + 1}
                </span>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => moveStep(idx, -1)}
                    disabled={idx === 0}
                    className="rounded p-0.5 text-[10px] text-gray-600 transition-colors hover:text-gray-300 disabled:opacity-30"
                    title="上移"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => moveStep(idx, 1)}
                    disabled={idx === steps.length - 1}
                    className="rounded p-0.5 text-[10px] text-gray-600 transition-colors hover:text-gray-300 disabled:opacity-30"
                    title="下移"
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    onClick={() => removeStep(idx)}
                    className="ml-1 rounded p-0.5 text-[10px] text-red-500/60 transition-colors hover:text-red-400"
                    title="删除步骤"
                  >
                    ✕
                  </button>
                </div>
              </div>

              {/* wait / send / timeout 字段 */}
              <div className="space-y-2">
                <div>
                  <label className="mb-0.5 block text-[10px] text-gray-600">等待模式（正则）</label>
                  <input
                    type="text"
                    value={step.wait}
                    onChange={(e) => updateStep(idx, "wait", e.target.value)}
                    placeholder="如 [Pp]assword: 或 ID\s*>"
                    className="w-full rounded border border-white/10 bg-white/5 px-2.5 py-1.5 font-mono text-xs text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
                  />
                </div>
                <div className="grid grid-cols-[1fr,80px] gap-2">
                  <div>
                    <label className="mb-0.5 block text-[10px] text-gray-600">发送内容</label>
                    <input
                      type="text"
                      value={step.send}
                      onChange={(e) => updateStep(idx, "send", e.target.value)}
                      placeholder="文本或 {{password}}"
                      className="w-full rounded border border-white/10 bg-white/5 px-2.5 py-1.5 font-mono text-xs text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-0.5 block text-[10px] text-gray-600">超时(s)</label>
                    <input
                      type="number"
                      value={step.timeout}
                      onChange={(e) => updateStep(idx, "timeout", parseFloat(e.target.value) || 10)}
                      min={1}
                      max={120}
                      className="w-full rounded border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-gray-200 focus:border-emerald-500/40 focus:outline-none"
                    />
                  </div>
                </div>
                {/* 变量提示 */}
                {step.send.includes("{{password}}") && (
                  <p className="text-[10px] text-cyan-500/70">
                    💡 运行时将替换为上方「入口密码」字段的值
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {steps.length > 0 && (
        <p className="mt-2 text-[10px] text-gray-600">
          执行顺序：发送入口值 → 依次执行各步骤（等待匹配 → 发送响应）→ 等待成功模式
        </p>
      )}
    </div>
  );
}

// ── 凭据引用选择器（ComboBox） ──────────────────

interface CredentialRefFieldProps {
  value: string;
  onChange: (v: string) => void;
}

function CredentialRefField({ value, onChange }: CredentialRefFieldProps) {
  const [options, setOptions] = useState<CredentialNameItem[]>([]);
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  // 懒加载：首次展开时获取凭据名称列表
  const loadOptions = useCallback(async () => {
    if (loaded) return;
    try {
      const names = await listCredentialNames();
      setOptions(names);
    } catch {
      // 静默忽略（降级为手动输入）
    }
    setLoaded(true);
  }, [loaded]);

  const handleFocus = useCallback(() => {
    loadOptions();
    setOpen(true);
  }, [loadOptions]);

  const handleSelect = useCallback((name: string) => {
    onChange(name);
    setOpen(false);
  }, [onChange]);

  return (
    <div className="relative">
      <label className="mb-1 block text-[11px] text-gray-500">凭据引用</label>
      <div className="flex gap-1">
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={handleFocus}
          onBlur={() => setTimeout(() => setOpen(false), 200)}
          placeholder="选择或输入凭据名称"
          className="flex-1 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500/40 focus:outline-none"
        />
        {value && (
          <button
            type="button"
            onClick={() => onChange("")}
            className="rounded-lg border border-white/10 bg-white/5 px-2 text-xs text-gray-500 transition-colors hover:text-gray-300"
            title="清除"
          >
            ✕
          </button>
        )}
      </div>
      {/* 下拉选项 */}
      {open && options.length > 0 && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-40 overflow-y-auto rounded-lg border border-white/10 bg-gray-950/95 py-1 shadow-xl backdrop-blur-xl">
          {options
            .filter((opt) => !value || opt.name.toLowerCase().includes(value.toLowerCase()))
            .map((opt) => (
              <button
                key={opt.name}
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => handleSelect(opt.name)}
                className={`flex w-full items-center justify-between px-3 py-1.5 text-left text-xs transition-colors hover:bg-white/5 ${
                  opt.name === value ? "text-emerald-300" : "text-gray-300"
                }`}
              >
                <span className="font-mono">{opt.name}</span>
                {opt.description && (
                  <span className="ml-2 truncate text-[10px] text-gray-600">{opt.description}</span>
                )}
              </button>
            ))}
          {options.filter((opt) => !value || opt.name.toLowerCase().includes(value.toLowerCase())).length === 0 && (
            <p className="px-3 py-2 text-[11px] text-gray-600">无匹配凭据（将作为新名称使用）</p>
          )}
        </div>
      )}
      <p className="mt-1 text-[10px] text-gray-600">
        引用共享凭据的密码，用于 {"{{password}}"} 变量替换。在「系统设置 → 凭据管理」中管理凭据。
      </p>
    </div>
  );
}

// ── 工具函数 ──────────────────────────────────

function hostMatchesSearch(host: Host, query: string): boolean {
  const q = query.toLowerCase();
  if (host.name.toLowerCase().includes(q)) return true;
  if (host.hostname.toLowerCase().includes(q)) return true;
  if (host.description?.toLowerCase().includes(q)) return true;
  if (host.tags.some((t) => t.toLowerCase().includes(q))) return true;
  return host.children?.some((child) => hostMatchesSearch(child, q)) ?? false;
}

function countChildren(host: Host): number {
  return (host.children ?? []).reduce((sum, child) => sum + 1 + countChildren(child), 0);
}

function countAll(hosts: Host[]): number {
  return hosts.reduce((sum, host) => sum + 1 + countAll(host.children ?? []), 0);
}
