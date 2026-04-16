"""排障脚本片段数据模型。

定义 Snippet 系统的所有 Pydantic Schema，供 SnippetRegistry、
REST API 和 MCP 工具共同使用。

层级关系：
  SnippetsConfig
    └─ SnippetDomain（领域，如 ES / K8s / MySQL / Redis）
         └─ SnippetCommand（命令）
              └─ SnippetParam（参数）
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SnippetParam(BaseModel):
    """命令参数定义"""

    name: str = Field(..., min_length=1, description="参数名称")
    description: str = Field(default="", description="参数说明")
    default: str = Field(default="", description="默认值（空字符串表示无默认值）")
    required: bool = Field(default=False, description="是否必填")


class SnippetCommand(BaseModel):
    """单个排障命令定义"""

    id: str = Field(..., min_length=1, description="命令唯一 ID（如 es、esl、ki）")
    name: str = Field(..., min_length=1, description="命令名称（用于 UI 展示）")
    description: str = Field(default="", description="命令说明")
    syntax: str = Field(default="", description="使用语法示例")
    template: str = Field(default="", description="命令模板，使用 {{param}} 占位符")
    timeout: int | None = Field(default=None, ge=1, le=600, description="命令级超时（秒），None 时回退到领域级")
    params: list[SnippetParam] = Field(default_factory=list, description="命令参数列表")


class SnippetDomain(BaseModel):
    """排障领域定义（如 Elasticsearch、Kubernetes 等）"""

    id: str = Field(..., min_length=1, description="领域唯一 ID（如 es、k8s、mysql）")
    name: str = Field(..., min_length=1, description="领域名称")
    icon: str = Field(default="📦", description="领域图标（emoji 或 icon class）")
    description: str = Field(default="", description="领域说明")
    script_file: str = Field(default="", description="脚本文件路径（相对于 config/ 目录）")
    script_sha256: str | None = Field(default=None, description="脚本文件 SHA256（可选完整性校验）")
    default_timeout: int | None = Field(default=None, ge=1, le=600, description="领域级默认超时（秒）")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    commands: list[SnippetCommand] = Field(default_factory=list, description="命令列表")


class SnippetsConfig(BaseModel):
    """Snippet 配置顶层结构（对应 snippets.yaml）"""

    domains: list[SnippetDomain] = Field(default_factory=list, description="领域列表")


class SnippetDomainSummary(BaseModel):
    """领域概要信息（列表 API 响应，不含命令详情）"""

    id: str
    name: str
    icon: str
    description: str
    tags: list[str]
    command_count: int
