"""AI employees on SimpleX Chat: chat accounts run by AI agents with skills."""

from .agent import Agent
from .config import AppConfig, EmployeeConfig, load_config
from .employee import Employee, Office
from .llm import LLM, AnthropicLLM
from .providers import ChatModel, ModelProfile, make_model
from .skills import Skill, SkillContext, SkillError, skill

__all__ = [
    "LLM",
    "Agent",
    "AnthropicLLM",
    "AppConfig",
    "ChatModel",
    "Employee",
    "EmployeeConfig",
    "ModelProfile",
    "Office",
    "Skill",
    "SkillContext",
    "SkillError",
    "load_config",
    "make_model",
    "skill",
]
