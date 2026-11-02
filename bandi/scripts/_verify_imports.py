import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

packages = [
    "bandi.agent",
    "bandi.agent.prompt_harness",
    "bandi.api",
    "bandi.cli",
    "bandi.company",
    "bandi.context",
    "bandi.context.sources",
    "bandi.core",
    "bandi.eval",
    "bandi.llm",
    "bandi.llm.adapters",
    "bandi.llm.providers",
    "bandi.memory",
    "bandi.memory.session",
    "bandi.observability",
    "bandi.ontology",
    "bandi.perception",
    "bandi.resources",
    "bandi.safety",
    "bandi.session",
    "bandi.skills",
    "bandi.tools",
]

failed = []
for p in packages:
    try:
        __import__(p)
    except Exception as e:  # noqa: BLE001
        failed.append(f"  {p}: {e}")

checks = [
    "from bandi.core import BandiError",
    "from bandi.llm import BaseProvider",
    "from bandi.agent import ExecutionContext",
    "from bandi.tools import BaseTool",
    "from bandi.company import Phase, OrgEngine",
    "from bandi.safety import PromptGuard",
    "from bandi.ontology import OntologyStore, StoreLimits",
    "from bandi.cli import UIStateStore",
    "from bandi.eval import BenchmarkRunner",
]
for c in checks:
    try:
        exec(c)
    except Exception as e:  # noqa: BLE001
        failed.append(f"  re-export: {c.split(' import ')[0]} -> {e}")

if failed:
    print("IMPORT FAILURES:")
    print("\n".join(failed))
    sys.exit(1)
else:
    print(f"All {len(packages)} packages + {len(checks)} re-exports OK")
