# 功能：GetHtmlCanvasGuideTool — 按需注入 HTML 页面设计指引（无副作用）
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_REFERENCE_TEMPLATE = """\
<main class="min-h-screen bg-white text-zinc-950">
  <div class="mx-auto max-w-6xl px-5 py-12 sm:px-8 lg:py-20">
    <header class="max-w-3xl">
      <p class="text-sm font-medium tracking-wide text-zinc-500">Context label</p>
      <h1 class="mt-3 text-4xl font-semibold tracking-tight sm:text-6xl">
        One clear promise, expressed in the product's own voice
      </h1>
      <p class="mt-5 max-w-2xl text-base leading-7 text-zinc-600 sm:text-lg">
        A concise explanation that gives the reader enough context to act.
      </p>
    </header>
    <section aria-labelledby="details" class="mt-12 border-t border-black/10 pt-8">
      <h2 id="details" class="text-xl font-semibold tracking-tight">What matters</h2>
      <div class="mt-6 grid gap-8 md:grid-cols-3">
        <article><h3 class="font-medium">Clear hierarchy</h3>
          <p class="mt-2 text-sm leading-6 text-zinc-600">
            Make the reading order obvious without decorating every block.
          </p></article>
        <article><h3 class="font-medium">Useful content</h3>
          <p class="mt-2 text-sm leading-6 text-zinc-600">
            Replace this example with specific, credible user-facing copy.
          </p></article>
      </div>
    </section>
  </div>
</main>
"""

_GUIDE: dict[str, Any] = {
    "purpose": (
        "Create a complete, credible webpage whose visual language follows the "
        "content, audience, and brand — not a house style. Quality comes from "
        "hierarchy, proportion, typography, alignment, restraint, and detail."
    ),
    "delivery": [
        "Compact pages: `canvas_publish(mode=html, html=...)` single call.",
        "Larger multi-file pages: `canvas_publish(mode=html, html_files={...}, html_bundle_entry=...)`.",
        "Prefer a full self-styled document so authored CSS is not clobbered.",
    ],
    "design_method": [
        "Infer the page's job first: audience, primary action, information order, tone.",
        "Choose one coherent visual direction. Default to light surface + dark text unless the brief asks for dark.",
        "Do not default to a SaaS dashboard, centered hero, blue gradient, glassmorphism, or rounded-card grid.",
        "Establish reading order before decoration: one dominant idea per viewport.",
        "Use the reference template only as structure; adapt layout to the brief.",
    ],
    "visual_quality": [
        "Layout: small spacing scale, consistent alignment, deliberate widths, CSS grid/flex.",
        "Typography: visible contrast between display/heading/body/metadata roles; ~45-75 chars per line.",
        "Color: derive semantic roles (background/surface/text/muted/accent/status) from the brief; check contrast.",
        "Shape/depth: borders/radii/shadows must communicate grouping — do not round or shadow everything.",
        "Content: specific labels, realistic values, no filler or unsupported claims.",
        "Media: preserve aspect ratio, set object-fit, useful alt text.",
        "Motion: only to explain state or reward action; keep subtle; honor prefers-reduced-motion; complete with JS off.",
    ],
    "responsive_accessibility": [
        "Semantic HTML + single-column mobile reading order; enhance at sm:/md:/lg: only where needed.",
        "Check 320px mobile, tablet/desktop widths, long labels, overflow.",
        "One h1, ordered headings, landmarks, native buttons/links, alt text, visible focus.",
        "Light color scheme default; use `dark:` variants only when intentionally supporting both.",
    ],
    "quality_gate": [
        "First viewport says what this is, why it matters, and what to do next.",
        "Hierarchy stays clear in grayscale; no repeated decorative device by habit.",
        "Copy is complete, specific, free of placeholders.",
        "No clipping, horizontal overflow, broken assets, or unreadable contrast.",
        "Works without JS; enabled interactions have useful states.",
    ],
    "reference_template": _REFERENCE_TEMPLATE,
}


class GetHtmlCanvasGuideTool(BaseTool):
    name: str = "get_html_canvas_guide"
    description: str = (
        "Return on-demand professional HTML authoring guidance for `canvas_publish`: "
        "delivery options, style-neutral design method, visual-quality and accessibility "
        "checks, and a structural template. Use for substantial or appearance-sensitive "
        "pages; skip for trivial HTML. Read-only, no side effects."
    )
    category: ToolCategory = ToolCategory.CANVAS
    feature_group: str = "canvas"
    capability_tags: list[str] = []
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        return {"success": True, "guide": _GUIDE}
