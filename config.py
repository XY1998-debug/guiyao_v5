"""QuantPilot 统一配置加载器 - 从 config.yaml 读取所有配置"""

import os
import re
import logging
from pathlib import Path
from functools import lru_cache

import yaml

logger = logging.getLogger("quantpilot.config")

PROJECT_ROOT = Path(__file__).parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _resolve_env_vars(value: str) -> str:
    """替换字符串中的 ${ENV_VAR} 为环境变量值"""
    if not isinstance(value, str):
        return value

    def _replace(m):
        var_name = m.group(1)
        env_val = os.environ.get(var_name, "")
        if not env_val:
            logger.warning(f"环境变量 {var_name} 未设置")
        return env_val

    return re.sub(r'\$\{(\w+)\}', _replace, value)


def _resolve_dict(d: dict) -> dict:
    """递归替换字典中所有字符串的环境变量"""
    resolved = {}
    for k, v in d.items():
        if isinstance(v, dict):
            resolved[k] = _resolve_dict(v)
        elif isinstance(v, list):
            resolved[k] = [_resolve_dict(i) if isinstance(i, dict)
                          else _resolve_env_vars(i) if isinstance(i, str)
                          else i for i in v]
        elif isinstance(v, str):
            resolved[k] = _resolve_env_vars(v)
        else:
            resolved[k] = v
    return resolved


@lru_cache(maxsize=1)
def load_config(path: str = None) -> dict:
    """加载配置文件，替换环境变量，返回配置字典"""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config = _resolve_dict(raw)
    logger.info(f"配置已加载: {config_path}")
    return config


def get_config() -> dict:
    """获取配置（已缓存）"""
    return load_config()


def update_config_value(key_path: str, value, save: bool = True) -> bool:
    """修改配置中的指定值
    
    Args:
        key_path: 点分路径，如 "llm.primary.model"
        value: 新值
        save: 是否保存到文件
    
    Returns:
        是否成功
    """
    config = load_config()
    keys = key_path.split(".")
    
    # 导航到目标位置
    target = config
    for key in keys[:-1]:
        if key not in target:
            target[key] = {}
        target = target[key]
    
    # 设置值
    old_value = target.get(keys[-1])
    target[keys[-1]] = value
    
    if save:
        config_path = DEFAULT_CONFIG_PATH
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"配置已更新: {key_path} = {value} (原值: {old_value})")
        # 清除缓存，下次重新加载
        load_config.cache_clear()
    
    return True


# 便捷访问属性
class ConfigProxy:
    """配置代理，支持属性访问"""
    
    def __init__(self):
        self._config = None
    
    def _ensure_loaded(self):
        if self._config is None:
            self._config = load_config()
    
    def __getattr__(self, name):
        self._ensure_loaded()
        if name.startswith('_'):
            return super().__getattribute__(name)
        return self._config.get(name)
    
    def get(self, key, default=None):
        self._ensure_loaded()
        return self._config.get(key, default)
    
    def reload(self):
        load_config.cache_clear()
        self._config = load_config()


# 全局配置实例
config = ConfigProxy()
