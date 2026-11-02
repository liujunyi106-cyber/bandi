# 第三方声明 / Third-Party Notices

本仓库包含或改编了以下开源项目的部分代码与设计。各部分版权归原作者所有，
按各自的许可证使用；本文件用于保留这些许可证要求的声明。

## LeAgent — Apache License 2.0

Copyright © Vixues

- 许可证全文：<https://www.apache.org/licenses/LICENSE-2.0>
- 涉及范围：`bandi/tools/util/` 下的文本/JSON/规则/日期工具实现，记忆衰减与
  工具选择的评分权重设计，episodic / working 记忆的语义划分。

## OpenOPC — MIT

- 涉及范围：公司调度中的工作项流转、用户修正（rework）语义与组织工作项规划的
  行为约定（按本仓库结构改写）。

## opencode — MIT

Copyright (c) 2025 opencode

- 涉及范围：上下文预算与工具输出 prune 的策略取舍、编辑匹配的回退链设计。

## StaffDeck — AGPL-3.0

- 涉及范围：知识库三层实体（document / bucket / chunk）、OKF 概念框架与引用链
  的设计思路。相关模块（`knowledge/`、`memory/distill.py`）已按本仓库自身实现重写。

## 说明

- 本仓库整体以 **AGPL-3.0-or-later** 发布（见 `LICENSE`）。
- Apache-2.0 与 MIT 均与 AGPL-3.0 兼容；上述声明用于满足其保留版权与许可
  声明的要求。
- 若某文件内另有独立许可证声明，以该文件为准。
