from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, JSON
from sqlalchemy.sql import func
from app.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="订阅名称")
    url = Column(Text, nullable=False, comment="订阅链接")
    tag = Column(String(50), nullable=True, comment="标签/备注（用于区分）")
    color = Column(String(20), nullable=True, default="#6c5ce7", comment="标识颜色")
    enabled = Column(Boolean, default=True, comment="是否启用")
    auto_update = Column(Boolean, default=True, comment="自动更新")
    update_interval = Column(Integer, default=86400, comment="更新间隔(秒)")
    last_update = Column(DateTime, nullable=True, comment="上次更新时间")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ProxyNode(Base):
    __tablename__ = "proxy_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(500), nullable=False, comment="节点名称")
    subscription_id = Column(Integer, nullable=True, comment="所属订阅ID")
    node_type = Column(String(50), nullable=False, comment="协议类型: ss/vmess/trojan/hysteria2/tuic等")
    server = Column(String(500), nullable=False)
    port = Column(Integer, nullable=False)
    config = Column(Text, nullable=False, comment="节点完整配置行(用于生成Surge配置)")
    region_auto = Column(String(100), nullable=True, comment="自动识别的地区")
    region_manual = Column(String(100), nullable=True, comment="手动指定的地区")
    pinned_services = Column(JSON, default=list, comment="手动钉选到的服务分组名列表")
    enabled = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, server_default=func.now())


class RegionGroup(Base):
    __tablename__ = "region_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, comment="地区名称（配置文件中显示）")
    display_name = Column(String(100), nullable=True, comment="显示名称（多语言）")
    icon_url = Column(Text, nullable=True, comment="图标URL")
    filter_regex = Column(String(500), nullable=True, comment="自动匹配正则")
    sort_order = Column(Integer, default=0, comment="排序")
    enabled = Column(Boolean, default=True)
    auto_enabled = Column(Boolean, default=True, server_default="1", comment="启用自动测速组")
    manual_enabled = Column(Boolean, default=True, server_default="1", comment="启用手动选择组")
    created_at = Column(DateTime, server_default=func.now())


class ServiceGroup(Base):
    __tablename__ = "service_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, comment="服务名称")
    display_name = Column(String(100), nullable=True, comment="显示名称（多语言）")
    icon_url = Column(Text, nullable=True)
    group_type = Column(String(20), default="select", comment="select/url-test")
    policies = Column(JSON, default=list, comment="策略列表")
    sort_order = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class ConfigProfile(Base):
    __tablename__ = "config_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, comment="配置名称")
    description = Column(Text, nullable=True, comment="配置描述")
    is_default = Column(Boolean, default=False, comment="是否为默认配置")
    locale = Column(String(10), default="zh", comment="语言: zh/en")
    final_action = Column(String(100), default="手动选择", comment="兜底规则策略: 手动选择/自动选择/代理/大陆直连等")
    general = Column(JSON, default=dict, comment="General段自定义配置")
    dns = Column(JSON, default=dict, comment="DNS配置")
    url_rewrites = Column(JSON, default=list, comment="URL重写规则")
    header_rewrites = Column(JSON, default=list, comment="Header重写规则")
    mitm = Column(JSON, default=dict, comment="MITM配置")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class CustomRule(Base):
    __tablename__ = "custom_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, nullable=True, comment="配置方案ID，NULL=默认规则")
    rule_text = Column(String(500), nullable=False, comment="规则文本，如 DOMAIN-SUFFIX,example.com,DIRECT")
    position = Column(String(20), default="general", comment="位置: pre(前置)/general(通用)")
    sort_order = Column(Integer, default=0, comment="排序")
    enabled = Column(Boolean, default=True, comment="是否启用")
    comment = Column(String(200), nullable=True, comment="备注说明")


class HostMapping(Base):
    __tablename__ = "host_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, nullable=True, comment="配置方案ID，NULL=默认映射")
    domain = Column(String(500), nullable=False, comment="域名，如 *.example.com")
    target = Column(String(200), nullable=False, comment="目标，如 server:223.5.5.5 或 IP地址")
    sort_order = Column(Integer, default=0, comment="排序")
    enabled = Column(Boolean, default=True, comment="是否启用")


class RuleSource(Base):
    __tablename__ = "rule_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="规则集名称")
    url = Column(Text, nullable=False, comment="规则集URL")
    action = Column(String(50), nullable=False, comment="动作: DIRECT/REJECT/策略组名")
    rule_type = Column(String(20), default="RULE-SET", comment="规则类型")
    category = Column(String(100), nullable=True, comment="分类")
    enabled = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
