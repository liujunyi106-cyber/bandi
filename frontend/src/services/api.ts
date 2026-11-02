/**
  * 功能：同源 fetch 客户端（@base docs/API契约.md / docs/前端设计规划.md）。
 * - 统一走 `/api`（dev 由 vite proxy 转到内核 8766，生产由 FastAPI 托管）
 * - FastAPI 422 校验错误体 `detail[{loc,msg}]` → `loc: msg` 可读文案
  * 统一错误体 `{code,message,detail,request_id}` 原样透出，便于 toast / 追踪
 * - 非 JSON / 空响应不崩，返回空对象
 */

import { desktopApiKey } from "@/lib/desktop";

const API_PREFIX = "/api";

/** 统一错误体（docs/API契约.md）。 */
export interface ApiErrorBody {
  code?: string;
  message?: string;
  detail?: unknown;
  request_id?: string;
}

/** 请求失败异常：携带 HTTP 状态与内核错误码。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly detail?: unknown;

  constructor(status: number, message: string, body?: ApiErrorBody) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.code;
    this.requestId = body?.request_id;
    this.detail = body?.detail;
  }
}

/** 功能：把 FastAPI 校验体 `detail[{loc,msg}]` 压成一行可读文案。 */
export function formatValidationDetail(detail: unknown): string | null {
  if (!Array.isArray(detail) || detail.length === 0) return null;
  const parts = detail.map((item) => {
    if (item && typeof item === "object" && "loc" in item && "msg" in item) {
      const rec = item as { loc?: unknown; msg?: unknown };
      const loc = Array.isArray(rec.loc)
        ? rec.loc.filter((seg) => seg !== "body" && seg !== "query").join(".")
        : "";
      return loc ? `${loc}: ${String(rec.msg)}` : String(rec.msg);
    }
    return typeof item === "string" ? item : JSON.stringify(item);
  });
  return parts.join("; ");
}

function resolveErrorMessage(status: number, body: ApiErrorBody): string {
  const validation = formatValidationDetail(body.detail);
  if (validation) return validation;
  if (typeof body.message === "string" && body.message.trim()) return body.message;
  if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  return `请求失败（HTTP ${status}）`;
}

/** 请求选项：`json` 自动序列化并带 Content-Type。 */
export interface RequestOptions extends Omit<RequestInit, "body"> {
  json?: unknown;
  body?: BodyInit | null;
}

/** 功能：发起一次同源 API 调用（自动补前缀、解析统一错误体、解 JSON）。 */
export async function apiFetch<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { json, headers, ...rest } = options;
  const url = path.startsWith(API_PREFIX) || /^https?:/.test(path) ? path : `${API_PREFIX}${path}`;

  const finalHeaders = new Headers(headers);
  let body: BodyInit | null | undefined = rest.body;
  if (json !== undefined) {
    finalHeaders.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  finalHeaders.set("Accept", "application/json");
  const apiKey = desktopApiKey();
  if (apiKey) finalHeaders.set("Authorization", `Bearer ${apiKey}`);

  let response: Response;
  try {
    response = await fetch(url, { ...rest, headers: finalHeaders, body });
  } catch (cause) {
    throw new ApiError(0, "内核未就绪或网络不可达", { detail: String(cause) });
  }

  const raw = await response.text();
  let parsed: unknown = undefined;
  if (raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = undefined;
    }
  }

  if (!response.ok) {
    const errorBody: ApiErrorBody =
      parsed && typeof parsed === "object"
        ? (parsed as ApiErrorBody)
        : { detail: raw || undefined };
    throw new ApiError(response.status, resolveErrorMessage(response.status, errorBody), errorBody);
  }

  return (parsed ?? {}) as T;
}

/** 功能：REST 动词糖（get/post/patch/del），全部返回已解析 JSON。 */
export const api = {
  get: <T = unknown>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "GET" }),
  post: <T = unknown>(path: string, json?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "POST", json }),
  patch: <T = unknown>(path: string, json?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "PATCH", json }),
  put: <T = unknown>(path: string, json?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "PUT", json }),
  del: <T = unknown>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "DELETE" }),
  /** 功能：multipart 上传（FormData；不设 Content-Type——浏览器自带 boundary）。 */
  postForm: <T = unknown>(path: string, form: FormData, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: "POST", body: form }),
};
