# 功能：TalentMarket<->OrgEngine 绑定端到端契约（防装配点遗漏复发）
# 契约原则：走生产主链路——custom_runtime 装配的 bundle 里 TalentMarket 已绑定 org_engine
# package_loader.install 把已装包写入同一 org_engine.installed_packages 后
# bundle 内 talent_market.list_installed_packages() 必须能读回（钉最终产物，不钉实现）。
from __future__ import annotations

from pathlib import Path

import yaml

from bandi.company.company_runtime_profiles import RoleProfile
from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
from bandi.company.package_loader import PackageLoader
from bandi.company.store import WorkItemStore


def _roles() -> list[RoleProfile]:
    return [
        RoleProfile(id="manager", name="Manager", reports_to=None, can_spawn=["worker"]),
        RoleProfile(id="worker", name="Worker", reports_to="manager"),
    ]


def _make_installable_pkg(tmp_path: Path) -> Path:
    """构造最小可安装包目录（通过沙箱校验，含合法 manifest）。"""
    pkg_dir = tmp_path / "src-pkg.opcpkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "manifest.yaml").write_text(
        yaml.dump(
            {
                "id": "endtoendpkg",
                "name": "End To End Pkg",
                "description": "d",
                "version": "1.0.0",
                "author": {"name": "tester"},
                "license": "MIT",
                "category": "general",
                "tags": [],
            }
        ),
        encoding="utf-8",
    )
    (pkg_dir / "org_config.yaml").write_text(
        yaml.dump({"roles": [{"id": "e2e-role", "name": "E2E Role", "reports_to": None}]}),
        encoding="utf-8",
    )
    return pkg_dir


def _build_loader_with_bundle_engine(bundle, tmp_path: Path) -> PackageLoader:
    """生产装配语义对齐 app.py:922-928——loader.org_engine 用 dispatcher 的 org_engine。"""
    loader = PackageLoader(home=tmp_path / "home")
    loader.org_engine = bundle.org_engine
    return loader


def test_contract_installed_package_readable_via_bundle_market(tmp_path) -> None:
    """契约：真实安装后，custom_runtime bundle 的 talent_market 必须能读回已装包。

    覆盖：绑定额结（custom_runtime.py bind）+ 安装写入（package_loader.install）
    + 读取产物（list_installed_packages），缺任一环则本测试红。
    """
    store = WorkItemStore(str(tmp_path / "e2e.db"))
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1", workspace=str(tmp_path)))
    bundle = runner.build_org_runtime(roles=_roles())

    loader = _build_loader_with_bundle_engine(bundle, tmp_path)
    pkg = loader.load_from_path(_make_installable_pkg(tmp_path))
    info = loader.install(pkg)

    installed = bundle.recruiter_planner.talent_market.list_installed_packages()
    assert installed, "安装后 bundle talent_market 必须读到已装包记录"
    assert any(getattr(rec, "package_id", "") == info.package_id for rec in installed), (
        f"已装包 {info.package_id} 必须能被 bundle talent_market 读回，实际 {installed}"
    )


def test_contract_bundle_market_bound_to_bundle_engine(tmp_path) -> None:
    """契约：bundle 内 talent_market 必须绑定到同一 org_engine，绑定对象恒等。"""
    store = WorkItemStore(str(tmp_path / "e2e.db"))
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))
    bundle = runner.build_org_runtime(roles=_roles())

    market = bundle.recruiter_planner.talent_market
    assert market._org_engine is bundle.org_engine
