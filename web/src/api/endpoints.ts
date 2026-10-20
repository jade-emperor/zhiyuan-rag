/**
 * 接口清单 —— 每个函数对应后端一条路由,路径与 `src/project_rag/api/app.py` 一一对应。
 *
 * 路径**全写在这里**,不在组件里拼:业务接口一律在 `/api/v1` 下(后端有命名空间契约),
 * 拼错一个前缀只会在运行时变成 404,而写在一处就能被静态检查。
 */

import { request } from './client'
import type {
  Capabilities,
  CaptchaOut,
  CostOut,
  CreateUserOut,
  DeletedSubjectOut,
  DeletedDocument,
  DeletedOut,
  DocumentAclOut,
  DocumentPreview,
  DocumentRecord,
  DocumentsOut,
  DeletedFaqOut,
  FaqEntry,
  FaqListOut,
  FolderDetail,
  FolderRecord,
  FoldersOut,
  GroupDocumentsOut,
  GroupOut,
  GroupsOut,
  HistoryOut,
  LoginOut,
  Me,
  MetricsOut,
  QueryRequest,
  QueryResponse,
  SessionsOut,
  StreamFrame,
  UserOut,
  UsersOut,
} from './types'

export const API_PREFIX = '/api/v1'

/** 流式问答那条 WebSocket 的路径(与 HTTP 同前缀,只有协议不同)。 */
export const STREAM_PATH = `${API_PREFIX}/stream`

export function capabilities(): Promise<Capabilities> {
  // 不含任何数据,所以**不带令牌**:没登录也要能拿到上传后缀(登录页的错误提示要用到)
  return request<Capabilities>(`${API_PREFIX}/capabilities`, { auth: false })
}

export function login(username: string, password: string): Promise<LoginOut> {
  return request<LoginOut>(`${API_PREFIX}/auth/login`, {
    method: 'POST',
    auth: false,
    body: { username, password },
  })
}

/**
 * 退出登录:**让服务端那枚令牌立刻失效**。
 *
 * 只删本地副本是不够的 —— 服务端那枚令牌照样有效到 TTL 结束(默认 12 小时),
 * 谁之前把它拷走了,"退出"对他没有任何作用。前端这一路是**尽力而为**:
 * 请求失败也照样清本地(不然用户就被卡在一个退不出去的界面里)。
 */
export function logout(): Promise<{ revoked: boolean }> {
  return request<{ revoked: boolean }>(`${API_PREFIX}/auth/logout`, { method: 'POST' })
}

export function me(): Promise<Me> {
  return request<Me>(`${API_PREFIX}/auth/me`)
}

export function sources(): Promise<{ sources: string[] }> {
  return request<{ sources: string[] }>(`${API_PREFIX}/sources`)
}

export function sessions(limit = 50): Promise<SessionsOut> {
  return request<SessionsOut>(`${API_PREFIX}/sessions?limit=${limit}`)
}

export function createSession(): Promise<{ session_id: string }> {
  return request<{ session_id: string }>(`${API_PREFIX}/create_session`, { method: 'POST' })
}

export function history(sessionId: string): Promise<HistoryOut> {
  return request<HistoryOut>(`${API_PREFIX}/history/${encodeURIComponent(sessionId)}`)
}

export function clearSession(sessionId: string): Promise<DeletedOut> {
  return request<DeletedOut>(`${API_PREFIX}/history/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

// ── 一级目录(知识库的两层结构)──────────────────────────────────────────
/** 全部目录 + 未分类那一格 + 汇总。读是「知识库」这一档(员工也能看)。 */
export function folders(): Promise<FoldersOut> {
  return request<FoldersOut>(`${API_PREFIX}/folders`)
}

/** 一个目录 + 里面的文档 + 汇总(二级页面只发这一条)。`unfiled` 也认。 */
export function folderDetail(folderId: string): Promise<FolderDetail> {
  return request<FolderDetail>(`${API_PREFIX}/folders/${encodeURIComponent(folderId)}`)
}

/** 新建目录(仅管理员)。重名 409、名字不合法 400 —— 两种错各有各的下一步。 */
export function createFolder(name: string, description = ''): Promise<FolderRecord> {
  return request<FolderRecord>(`${API_PREFIX}/folders`, {
    method: 'POST',
    body: { name, description },
  })
}

/** 改名 / 改说明(仅管理员)。**改名不改 ID**,目录里的文档不会脱钩。 */
export function updateFolder(
  folderId: string,
  patch: { name?: string; description?: string },
): Promise<FolderRecord> {
  return request<FolderRecord>(`${API_PREFIX}/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH',
    body: patch,
  })
}

/** 删一个**空**目录(仅管理员)。里面还有文档时 409,回执里带份数。 */
export function deleteFolder(folderId: string): Promise<{ folder_id: string; deleted: number }> {
  return request<{ folder_id: string; deleted: number }>(
    `${API_PREFIX}/folders/${encodeURIComponent(folderId)}`,
    { method: 'DELETE' },
  )
}

/** 把一份文档挪到另一个目录(`folderId` 给空串 = 挪回未分类)。仅管理员,只动归属。 */
export function moveDocument(
  documentId: string,
  folderId: string,
): Promise<{ document_id: string; folder: string }> {
  return request<{ document_id: string; folder: string }>(
    `${API_PREFIX}/documents/${encodeURIComponent(documentId)}`,
    { method: 'PATCH', body: { folder_id: folderId } },
  )
}

// ── 文档治理(知识库管理页)──────────────────────────────────────────────
/**
 * 文档清单。`folder` 给目录 ID(或 `unfiled`)就只看那一格;给空串 = 全部。
 *
 * 两条**分开写**而不是拼一个 `${query}`:拼接会让"路径"这个模板字符串在 `?` 之前
 * 就出现一个 `${…}`,而 `check_m7_frontend` 正是靠"`${API_PREFIX}` 后面到 `?` 为止"
 * 来认路径的 —— 那样它读到的是 `/documents{}`(一条不存在的路由)。
 */
export function documents(folder = ''): Promise<DocumentsOut> {
  if (!folder) return request<DocumentsOut>(`${API_PREFIX}/documents`)
  return request<DocumentsOut>(`${API_PREFIX}/documents?folder=${encodeURIComponent(folder)}`)
}

/**
 * 上传一份文档并**异步入库**。
 *
 * 两个刻意的形状:
 * 1. **文件走原始 body、名字走 query**(`?filename=`)—— 不用 `multipart/form-data`,
 *    那要后端多一个依赖;而浏览器的 `fetch(url, {body: file})` 原生就发原始字节;
 * 2. 返回的是 `202 + pending`,入库在后台跑(539 页 PDF 在 CPU 上要 8 分钟)。
 *    **拿到 202 不等于入库完成** —— 界面靠轮询清单看状态。
 *
 * `folder` 是这一份要进的**那一格**(二级页面把当前目录带上;空 = 未分类)。
 * 目录不存在时后端 404 —— 不自动新建,名字写错一个字的后果该是当场被看见。
 */
export function uploadDocument(file: File, folder = ''): Promise<DocumentRecord> {
  const target = folder ? `&folder=${encodeURIComponent(folder)}` : ''
  return request<DocumentRecord>(
    `${API_PREFIX}/documents?filename=${encodeURIComponent(file.name)}${target}`,
    {
      method: 'POST',
      raw: file,
      headers: { 'Content-Type': 'application/octet-stream' },
    },
  )
}

/** 删除一份文档:登记行 + 向量库里的块 + 磁盘原文件三处一起清(仅管理员,幂等)。 */
export function deleteDocument(documentId: string): Promise<DeletedDocument> {
  return request<DeletedDocument>(
    `${API_PREFIX}/documents/${encodeURIComponent(documentId)}`,
    { method: 'DELETE' },
  )
}

/** 预览:这份文档入库后是什么样(按**父块**折叠,带页码)。 */
export function documentPreview(
  documentId: string,
  limit = 20,
  chars = 400,
): Promise<DocumentPreview> {
  return request<DocumentPreview>(
    `${API_PREFIX}/documents/${encodeURIComponent(documentId)}/chunks?limit=${limit}&chars=${chars}`,
  )
}

// ── 报表(延迟看板 + 消耗)───────────────────────────────────────────────
/**
 * 延迟报表(**运维与管理员**)。`window` 的白名单与 `top` 的上限都来自
 * `capabilities.metrics` —— 前端不写死,否则后端改了窗口这里会拿着一个 400 的窗口名去打。
 */
export function metrics(window: string, top = 5): Promise<MetricsOut> {
  return request<MetricsOut>(
    `${API_PREFIX}/reports/metrics?window=${encodeURIComponent(window)}&top=${top}`,
  )
}

/**
 * 消耗报表(**运维与管理员**):窗口内调了几次模型、烧了多少 token、折合多少钱。
 *
 * 权限与延迟报表**完全一样**、窗口白名单也是同一张表 —— 两份报表要能对着看。
 * 钱算不出来时后端如实说(`unpriced_models` / `unmetered_calls`),界面照原样显示。
 */
export function cost(window: string): Promise<CostOut> {
  return request<CostOut>(`${API_PREFIX}/reports/cost?window=${encodeURIComponent(window)}`)
}

// ── 账号与可见范围(仅管理员)────────────────────────────────────────────
/** 全部账号(用户名 / 角色 / 所属组)。**不含密码**。 */
export function adminUsers(): Promise<UsersOut> {
  return request<UsersOut>(`${API_PREFIX}/admin/users`)
}

/** 建一个账号。角色是 `employee` / `ops` / `admin`;密码只进不出。 */
export function adminCreateUser(payload: {
  username: string
  password: string
  role: string
}): Promise<CreateUserOut> {
  return request<CreateUserOut>(`${API_PREFIX}/admin/users`, { method: 'POST', body: payload })
}

/** 删一个账号(不能删自己、不能删最后一个管理员 —— 后端 400 时界面照原样显示原因)。 */
export function adminDeleteUser(
  userId: string,
): Promise<DeletedSubjectOut & { user_id: string; username: string }> {
  return request<DeletedSubjectOut & { user_id: string; username: string }>(
    `${API_PREFIX}/admin/users/${encodeURIComponent(userId)}`,
    { method: 'DELETE' },
  )
}

/** 替某个账号改密码。 */
export function adminResetPassword(userId: string, password: string): Promise<UserOut> {
  return request<UserOut>(`${API_PREFIX}/admin/users/${encodeURIComponent(userId)}/password`, {
    method: 'POST',
    body: { password },
  })
}

/** 全部组(一类员工)与各自的成员。 */
export function adminGroups(): Promise<GroupsOut> {
  return request<GroupsOut>(`${API_PREFIX}/admin/groups`)
}

export function adminCreateGroup(name: string): Promise<GroupOut> {
  return request<GroupOut>(`${API_PREFIX}/admin/groups`, { method: 'POST', body: { name } })
}

export function adminDeleteGroup(
  groupId: string,
): Promise<DeletedSubjectOut & { group_id: string }> {
  return request<DeletedSubjectOut & { group_id: string }>(
    `${API_PREFIX}/admin/groups/${encodeURIComponent(groupId)}`,
    { method: 'DELETE' },
  )
}

/** **整份**设置成员名单(声明式:勾完点保存,语义就是"这些人")。 */
export function adminSetMembers(groupId: string, userIds: string[]): Promise<GroupOut> {
  return request<GroupOut>(
    `${API_PREFIX}/admin/groups/${encodeURIComponent(groupId)}/members`,
    { method: 'PUT', body: { user_ids: userIds } },
  )
}

/** **这个组能看哪些文档**(组侧授权页的读数,三态:人人可见 / 已授权 / 看不到)。 */
export function adminGroupDocuments(groupId: string): Promise<GroupDocumentsOut> {
  return request<GroupDocumentsOut>(
    `${API_PREFIX}/admin/groups/${encodeURIComponent(groupId)}/documents`,
  )
}

/**
 * **整份**设置"这个组能看的文档"(声明式)。
 *
 * 两条边界由后端定、界面照着说:人人可见的文档在这里动不了(那要走文档侧可见范围);
 * 取消授权后名单变空的文档会回到人人可见,出参的 `opened` 会点名它们。
 */
export function adminSetGroupDocuments(
  groupId: string,
  allowed: string[],
): Promise<GroupDocumentsOut> {
  return request<GroupDocumentsOut>(
    `${API_PREFIX}/admin/groups/${encodeURIComponent(groupId)}/documents`,
    { method: 'PUT', body: { allowed } },
  )
}

/** 一份文档的可见范围(授权给了哪些人、哪些组)。 */
export function adminDocumentAcl(documentId: string): Promise<DocumentAclOut> {
  return request<DocumentAclOut>(
    `${API_PREFIX}/admin/documents/${encodeURIComponent(documentId)}/acl`,
  )
}

/** **整份**设置可见范围。两个名单都空 = 取消限制(回到人人可见)。 */
export function adminSetDocumentAcl(
  documentId: string,
  payload: { user_ids: string[]; group_ids: string[] },
): Promise<DocumentAclOut> {
  return request<DocumentAclOut>(
    `${API_PREFIX}/admin/documents/${encodeURIComponent(documentId)}/acl`,
    { method: 'PUT', body: payload },
  )
}

// ── FAQ 管理台(仅管理员)──────────────────────────────────────────────────
/**
 * 问答对清单。`query` 在服务端对问题与答案做包含匹配 —— 不是前端过滤:
 * 答案正文可能上千字,全量搬给浏览器只为搜一次不划算。
 */
export function faqEntries(query = ''): Promise<FaqListOut> {
  // 两个分支分开写：模板里 `${}` 出现在 `?` 之前，会让"路径清单对账"那条契约把它读成
  // `/admin/faq{}`（一条不存在的路由）。与 `documents(folder)` 同一条写法。
  if (!query) return request<FaqListOut>(`${API_PREFIX}/admin/faq`)
  return request<FaqListOut>(`${API_PREFIX}/admin/faq?query=${encodeURIComponent(query)}`)
}

/** 新增一条。空问题/超长 400、同问重复 409(回执点名已有 ID)。 */
export function createFaq(question: string, answer: string): Promise<FaqEntry> {
  return request<FaqEntry>(`${API_PREFIX}/admin/faq`, {
    method: 'POST',
    body: { question, answer },
  })
}

/** 改一条(字段可省;一个都不给 = 400)。 */
export function updateFaq(
  entryId: number,
  patch: { question?: string; answer?: string },
): Promise<FaqEntry> {
  return request<FaqEntry>(`${API_PREFIX}/admin/faq/${entryId}`, {
    method: 'PATCH',
    body: patch,
  })
}

/** 删一条(幂等:不存在的给 deleted=0,不报错)。 */
export function deleteFaq(entryId: number): Promise<DeletedFaqOut> {
  return request<DeletedFaqOut>(`${API_PREFIX}/admin/faq/${entryId}`, { method: 'DELETE' })
}

/** 非流式问答。**界面走 WebSocket**;这条留给"不要流式"的调用方与排障。 */
export function query(payload: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>(`${API_PREFIX}/query`, { method: 'POST', body: payload })
}

export type { StreamFrame }

export function captcha(): Promise<CaptchaOut> {
  return request<CaptchaOut>(`${API_PREFIX}/auth/captcha`, { auth: false })
}

export interface RegisterPayload {
  username: string
  password: string
  captcha_ticket: string
  captcha_answer: string
  invite_code?: string
}

export function register(payload: RegisterPayload): Promise<LoginOut> {
  return request<LoginOut>(`${API_PREFIX}/auth/register`, {
    method: "POST",
    auth: false,
    body: payload,
  })
}

export function changePassword(oldPassword: string, newPassword: string): Promise<LoginOut> {
  return request<LoginOut>(`${API_PREFIX}/auth/password`, {
    method: "POST",
    body: { old_password: oldPassword, new_password: newPassword },
  })
}
