/**
 * SnippetPanel — 排障脚本面板组件
 *
 * 提供领域选择 → 脚本加载 → 命令选择 → 参数填写 → 一键执行的完整工作流。
 *
 * 设计原则：
 *  - 高内聚：所有 Snippet 交互逻辑集中在此组件
 *  - 低耦合：只通过 sendInput 回调与终端通信，不直接操作 WebSocket
 *  - 数据与视图分离：API 调用集中在 useSnippetData hook 中
 */

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import type {
  SnippetDomainSummary,
  SnippetDomainDetail,
  SnippetCommand,
  SnippetParam,
} from "../services/api";
import {
  fetchSnippetDomains,
  fetchSnippetDomain,
  fetchSnippetScript,
  resolveSnippetTemplate,
  validateSnippetParams,
} from "../services/api";

// ── 类型 ────────────────────────────────────

interface SnippetPanelProps {
  /** 是否显示面板 */
  visible: boolean;
  /** 关闭面板回调 */
  onClose: () => void;
  /** 向终端发送输入（注入命令） */
  sendInput: (data: string) => void;
  /** 终端是否已连接 */
  isConnected: boolean;
}

/** 脚本加载状态 */
type LoadStatus = "idle" | "loading" | "loaded" | "error";

// ── 数据 Hook ───────────────────────────────

function useSnippetData() {
  const [domains, setDomains] = useState<SnippetDomainSummary[]>([]);
  const [activeDomain, setActiveDomain] = useState<SnippetDomainDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** 加载领域列表 */
  const loadDomains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSnippetDomains();
      setDomains(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载领域列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  /** 选择领域（加载详情） */
  const selectDomain = useCallback(async (domainId: string) => {
    setLoading(true);
    setError(null);
    try {
      const detail = await fetchSnippetDomain(domainId);
      setActiveDomain(detail);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载领域详情失败");
    } finally {
      setLoading(false);
    }
  }, []);

  /** 返回领域列表 */
  const clearActiveDomain = useCallback(() => {
    setActiveDomain(null);
  }, []);

  return { domains, activeDomain, loading, error, loadDomains, selectDomain, clearActiveDomain };
}

// ── 主组件 ──────────────────────────────────

export default function SnippetPanel({
  visible,
  onClose,
  sendInput,
  isConnected,
}: SnippetPanelProps) {
  const {
    domains,
    activeDomain,
    loading,
    error,
    loadDomains,
    selectDomain,
    clearActiveDomain,
  } = useSnippetData();

  const [scriptStatus, setScriptStatus] = useState<LoadStatus>("idle");
  const [selectedCommand, setSelectedCommand] = useState<SnippetCommand | null>(null);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 面板显示时加载领域列表
  useEffect(() => {
    if (visible && domains.length === 0) {
      loadDomains();
    }
  }, [visible, domains.length, loadDomains]);

  // 切换领域时重置状态
  useEffect(() => {
    setScriptStatus("idle");
    setSelectedCommand(null);
    setParamValues({});
    setValidationErrors([]);
  }, [activeDomain?.id]);

  // 选择命令时初始化参数默认值
  useEffect(() => {
    if (!selectedCommand) {
      setParamValues({});
      setValidationErrors([]);
      return;
    }
    const defaults: Record<string, string> = {};
    for (const p of selectedCommand.params) {
      if (p.default) defaults[p.name] = p.default;
    }
    setParamValues(defaults);
    setValidationErrors([]);
  }, [selectedCommand]);

  const showToast = useCallback((msg: string, duration = 2000) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast(msg);
    toastTimerRef.current = setTimeout(() => setToast(null), duration);
  }, []);

  /** 加载脚本到远端 */
  const handleLoadScript = useCallback(async () => {
    if (!activeDomain || !isConnected) return;
    setScriptStatus("loading");
    try {
      const loader = await fetchSnippetScript(activeDomain.id);
      // 发送 heredoc 加载命令到终端
      sendInput(loader.heredoc_loader + "\n");
      setScriptStatus("loaded");
      showToast(`${activeDomain.name} 脚本已加载`);
    } catch (err) {
      setScriptStatus("error");
      showToast(err instanceof Error ? err.message : "脚本加载失败", 3000);
    }
  }, [activeDomain, isConnected, sendInput, showToast]);

  /** 执行命令 */
  const handleExecute = useCallback(() => {
    if (!selectedCommand || !isConnected) return;

    // 校验必填参数
    const missing = validateSnippetParams(selectedCommand.params, paramValues);
    if (missing.length > 0) {
      setValidationErrors(missing);
      showToast(`请填写必填参数: ${missing.join(", ")}`, 3000);
      return;
    }
    setValidationErrors([]);

    // 解析模板
    const resolved = resolveSnippetTemplate(
      selectedCommand.template,
      selectedCommand.params,
      paramValues,
    );

    // 发送命令到终端
    sendInput(resolved + "\n");
    showToast(`已执行: ${resolved}`);
  }, [selectedCommand, paramValues, isConnected, sendInput, showToast]);

  /** 预览解析后的命令 */
  const previewCommand = useMemo(() => {
    if (!selectedCommand) return "";
    return resolveSnippetTemplate(
      selectedCommand.template,
      selectedCommand.params,
      paramValues,
    );
  }, [selectedCommand, paramValues]);

  if (!visible) return null;

  return (
    <div className="flex flex-col h-full border-t border-white/8 bg-gray-950/95 backdrop-blur-xl">
      {/* 标题栏 */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-white/8">
        <div className="flex items-center gap-2">
          {activeDomain && (
            <button
              type="button"
              onClick={clearActiveDomain}
              className="text-gray-500 hover:text-gray-300 transition-colors text-xs"
              title="返回领域列表"
            >
              ← 返回
            </button>
          )}
          <h3 className="text-xs font-medium text-gray-300">
            {activeDomain ? (
              <span className="flex items-center gap-1.5">
                <span>{activeDomain.icon}</span>
                <span>{activeDomain.name}</span>
                {scriptStatus === "loaded" && (
                  <span className="text-emerald-400 text-[10px]">✓ 已加载</span>
                )}
              </span>
            ) : (
              "排障脚本"
            )}
          </h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-gray-600 hover:text-gray-400 transition-colors text-sm"
          title="关闭面板"
        >
          ✕
        </button>
      </div>

      {/* 内容区域 */}
      <div className="flex-1 min-h-0 overflow-y-auto snippet-scroll">
        {loading ? (
          <_LoadingState />
        ) : error ? (
          <_ErrorState message={error} onRetry={activeDomain ? () => selectDomain(activeDomain.id) : loadDomains} />
        ) : !activeDomain ? (
          <_DomainList domains={domains} onSelect={selectDomain} />
        ) : (
          <_CommandPanel
            domain={activeDomain}
            scriptStatus={scriptStatus}
            selectedCommand={selectedCommand}
            paramValues={paramValues}
            validationErrors={validationErrors}
            previewCommand={previewCommand}
            isConnected={isConnected}
            onLoadScript={handleLoadScript}
            onSelectCommand={setSelectedCommand}
            onParamChange={(name, value) =>
              setParamValues((prev) => ({ ...prev, [name]: value }))
            }
            onExecute={handleExecute}
          />
        )}
      </div>

      {/* Toast */}
      {toast && (
        <div className="absolute bottom-2 left-1/2 -translate-x-1/2 z-30 px-3 py-1.5 bg-gray-800/95 border border-white/10 text-gray-200 text-[11px] rounded-lg shadow-xl pointer-events-none animate-pulse">
          {toast}
        </div>
      )}
    </div>
  );
}

// ── 子组件：领域列表 ────────────────────────

function _DomainList({
  domains,
  onSelect,
}: {
  domains: SnippetDomainSummary[];
  onSelect: (id: string) => void;
}) {
  if (domains.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600 text-xs py-8">
        暂无可用的排障脚本
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-2 p-3">
      {domains.map((domain) => (
        <button
          key={domain.id}
          type="button"
          onClick={() => onSelect(domain.id)}
          className="group flex flex-col items-start gap-1 rounded-lg border border-white/6 bg-white/3 p-2.5 text-left transition-all hover:border-emerald-500/30 hover:bg-emerald-500/5"
        >
          <div className="flex items-center gap-2 w-full">
            <span className="text-base">{domain.icon}</span>
            <span className="text-xs font-medium text-gray-200 truncate">{domain.name}</span>
          </div>
          <p className="text-[10px] text-gray-500 line-clamp-2 leading-relaxed">
            {domain.description}
          </p>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className="text-[10px] text-gray-600">{domain.command_count} 个命令</span>
            {domain.tags.slice(0, 2).map((tag) => (
              <span
                key={tag}
                className="rounded-full bg-white/5 px-1.5 py-0.5 text-[9px] text-gray-500"
              >
                {tag}
              </span>
            ))}
          </div>
        </button>
      ))}
    </div>
  );
}

// ── 子组件：命令面板 ────────────────────────

function _CommandPanel({
  domain,
  scriptStatus,
  selectedCommand,
  paramValues,
  validationErrors,
  previewCommand,
  isConnected,
  onLoadScript,
  onSelectCommand,
  onParamChange,
  onExecute,
}: {
  domain: SnippetDomainDetail;
  scriptStatus: LoadStatus;
  selectedCommand: SnippetCommand | null;
  paramValues: Record<string, string>;
  validationErrors: string[];
  previewCommand: string;
  isConnected: boolean;
  onLoadScript: () => void;
  onSelectCommand: (cmd: SnippetCommand | null) => void;
  onParamChange: (name: string, value: string) => void;
  onExecute: () => void;
}) {
  return (
    <div className="flex flex-col gap-2 p-3">
      {/* 脚本加载区 */}
      {scriptStatus !== "loaded" && (
        <div className="flex items-center gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2">
          <span className="text-amber-400 text-xs">⚡</span>
          <span className="text-[11px] text-amber-300/80 flex-1">
            需先加载 {domain.name} 脚本到远端
          </span>
          <button
            type="button"
            onClick={onLoadScript}
            disabled={!isConnected || scriptStatus === "loading"}
            className={`rounded px-2.5 py-1 text-[11px] font-medium transition-colors ${
              !isConnected
                ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                : scriptStatus === "loading"
                  ? "bg-amber-500/20 text-amber-400 cursor-wait"
                  : "bg-amber-500/20 text-amber-300 hover:bg-amber-500/30"
            }`}
          >
            {scriptStatus === "loading" ? (
              <span className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 animate-spin rounded-full border border-amber-400 border-t-transparent" />
                加载中
              </span>
            ) : scriptStatus === "error" ? (
              "重试加载"
            ) : (
              "加载脚本"
            )}
          </button>
        </div>
      )}

      {/* 命令列表 */}
      <div className="space-y-1">
        <h4 className="text-[10px] uppercase tracking-wider text-gray-600 px-0.5">
          可用命令 ({domain.commands.length})
        </h4>
        {domain.commands.map((cmd) => {
          const isSelected = selectedCommand?.id === cmd.id;
          return (
            <button
              key={cmd.id}
              type="button"
              onClick={() => onSelectCommand(isSelected ? null : cmd)}
              className={`w-full text-left rounded-md border px-2.5 py-2 transition-all ${
                isSelected
                  ? "border-emerald-500/40 bg-emerald-500/8"
                  : "border-white/6 bg-white/2 hover:border-white/12 hover:bg-white/4"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <code className="text-[11px] font-mono text-emerald-400">{cmd.id}</code>
                    <span className="text-[11px] text-gray-400">·</span>
                    <span className="text-[11px] text-gray-300 truncate">{cmd.name}</span>
                  </div>
                  <p className="text-[10px] text-gray-600 mt-0.5 truncate">{cmd.description}</p>
                </div>
                {cmd.params.length > 0 && (
                  <span className="shrink-0 text-[9px] text-gray-600 bg-white/5 rounded px-1 py-0.5">
                    {cmd.params.length} 参数
                  </span>
                )}
              </div>

              {/* 语法提示 */}
              {isSelected && (
                <div className="mt-1.5 text-[10px] text-gray-500 font-mono bg-black/30 rounded px-2 py-1">
                  {cmd.syntax}
                </div>
              )}
            </button>
          );
        })}
      </div>

      {/* 参数输入区 + 执行按钮 */}
      {selectedCommand && (
        <_ParamForm
          command={selectedCommand}
          paramValues={paramValues}
          validationErrors={validationErrors}
          previewCommand={previewCommand}
          isConnected={isConnected}
          onParamChange={onParamChange}
          onExecute={onExecute}
        />
      )}
    </div>
  );
}

// ── 子组件：参数表单 ────────────────────────

function _ParamForm({
  command,
  paramValues,
  validationErrors,
  previewCommand,
  isConnected,
  onParamChange,
  onExecute,
}: {
  command: SnippetCommand;
  paramValues: Record<string, string>;
  validationErrors: string[];
  previewCommand: string;
  isConnected: boolean;
  onParamChange: (name: string, value: string) => void;
  onExecute: () => void;
}) {
  const hasParams = command.params.length > 0;

  return (
    <div className="rounded-lg border border-white/8 bg-white/2 p-2.5 space-y-2">
      {/* 参数输入 */}
      {hasParams && (
        <div className="space-y-1.5">
          <h4 className="text-[10px] uppercase tracking-wider text-gray-600">参数</h4>
          {command.params.map((param) => {
            const isError = validationErrors.includes(param.name);
            return (
              <_ParamInput
                key={param.name}
                param={param}
                value={paramValues[param.name] ?? ""}
                isError={isError}
                onChange={(value) => onParamChange(param.name, value)}
                onEnter={onExecute}
              />
            );
          })}
        </div>
      )}

      {/* 命令预览 */}
      <div className="rounded bg-black/40 px-2.5 py-1.5">
        <div className="text-[10px] text-gray-600 mb-0.5">预览</div>
        <code className="text-[11px] font-mono text-emerald-300 break-all">{previewCommand}</code>
      </div>

      {/* 执行按钮 */}
      <button
        type="button"
        onClick={onExecute}
        disabled={!isConnected}
        className={`w-full rounded-md py-1.5 text-xs font-medium transition-colors ${
          !isConnected
            ? "bg-gray-700 text-gray-500 cursor-not-allowed"
            : "bg-emerald-600/80 text-white hover:bg-emerald-600 active:bg-emerald-700"
        }`}
      >
        {isConnected ? "执行命令 ↵" : "终端未连接"}
      </button>
    </div>
  );
}

// ── 子组件：单个参数输入 ─────────────────────

function _ParamInput({
  param,
  value,
  isError,
  onChange,
  onEnter,
}: {
  param: SnippetParam;
  value: string;
  isError: boolean;
  onChange: (value: string) => void;
  onEnter: () => void;
}) {
  return (
    <div>
      <label className="flex items-center gap-1 text-[10px] text-gray-400 mb-0.5">
        <span className="font-mono">{param.name}</span>
        {param.required && <span className="text-red-400">*</span>}
        <span className="text-gray-600">— {param.description}</span>
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onEnter();
        }}
        placeholder={param.default || `输入 ${param.name}`}
        className={`w-full rounded border bg-black/30 px-2 py-1 text-[11px] font-mono text-gray-200 placeholder-gray-700 outline-none transition-colors ${
          isError
            ? "border-red-500/50 focus:border-red-400"
            : "border-white/8 focus:border-emerald-500/40"
        }`}
      />
    </div>
  );
}

// ── 子组件：加载状态 ────────────────────────

function _LoadingState() {
  return (
    <div className="flex items-center justify-center py-8">
      <div className="flex items-center gap-2 text-gray-500 text-xs">
        <span className="inline-block h-3 w-3 animate-spin rounded-full border border-gray-600 border-t-emerald-400" />
        加载中...
      </div>
    </div>
  );
}

// ── 子组件：错误状态 ────────────────────────

function _ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-8 gap-2">
      <span className="text-red-400 text-xs">{message}</span>
      <button
        type="button"
        onClick={onRetry}
        className="text-[11px] text-gray-400 hover:text-emerald-400 transition-colors"
      >
        重试
      </button>
    </div>
  );
}
