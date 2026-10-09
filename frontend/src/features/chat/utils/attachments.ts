/* ===== 附件输入草稿（plan-import-adjustment-contract §1、§2、§3、§10） ===== */

import type { AttachmentInputWire, AttachmentWire } from "@/lib/contract";
import {
  ATTACHMENT_TOTAL_BYTES,
  hasAttachmentExtension,
  isObject,
  parseAttachmentInputWire,
  requireAttachmentName,
  requireNonNegativeInt,
  requireUuid,
} from "@/lib/business";

/**
 * 输入框与编辑框持有的一个附件：稳定 UUID、原文件名、原始字节数与序列化后的请求输入。
 * 上传项携带 Base64 原始字节，保留项只携带同会话附件 ID；两者都保留文件名供展示与标题使用。
 * 已提交消息的附件同样以该形状进入展示层，编辑时默认全部作为保留项载入。
 */
export interface AttachmentDraft {
  attachment_id: string;
  file_name: string;
  size_bytes: number;
  input: AttachmentInputWire;
}

/** 逐块转换避免一次性展开超长参数 */
const BINARY_CHUNK = 8192;

/** 原始字节 → 标准 Base64（§2）：字节内容保持原值，不做任何转换 */
function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let at = 0; at < bytes.length; at += BINARY_CHUNK) {
    binary += String.fromCharCode(
      ...bytes.subarray(at, Math.min(at + BINARY_CHUNK, bytes.length)),
    );
  }
  return btoa(binary);
}

/** 严格 UTF-8 校验（§2）：非法字节明确报错，不替换字符、不自动转换编码 */
function requireUtf8(bytes: Uint8Array, fileName: string): void {
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error(`${fileName} 不是 UTF-8 编码的文本。`);
  }
}

/** 最终附件集合的原始字节合计（§1）：编辑时包含保留项 */
export function attachmentBytes(drafts: readonly AttachmentDraft[]): number {
  return drafts.reduce((total, item) => total + item.size_bytes, 0);
}

/**
 * 导入与拖拽共用入口：按给定顺序校验文件名、扩展名、剩余字节额度与 UTF-8，
 * 全部通过才追加进草稿集合；任一文件不合格即整次操作不改变原草稿。
 */
export async function appendAttachmentFiles(
  drafts: readonly AttachmentDraft[],
  files: readonly File[],
): Promise<AttachmentDraft[]> {
  const prepared: AttachmentDraft[] = [];
  let total = attachmentBytes(drafts);
  for (const file of files) {
    const fileName = requireAttachmentName(file.name, "文件名");
    if (!hasAttachmentExtension(fileName))
      throw new Error(`${fileName} 仅支持 .md 与 .txt 文件。`);
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (total + bytes.length > ATTACHMENT_TOTAL_BYTES)
      throw new Error(
        `附件合计 ${total + bytes.length} 字节，超过 ${ATTACHMENT_TOTAL_BYTES} 字节上限。`,
      );
    requireUtf8(bytes, fileName);
    const attachmentId = crypto.randomUUID();
    prepared.push({
      attachment_id: attachmentId,
      file_name: fileName,
      size_bytes: bytes.length,
      input: {
        kind: "upload",
        attachment_id: attachmentId,
        file_name: fileName,
        data_base64: bytesToBase64(bytes),
      },
    });
    total += bytes.length;
  }
  return [...drafts, ...prepared];
}

/** 编辑默认保留原附件（§3.2）：按历史顺序恢复为引用项，移除即从草稿删除，替换为删除后追加上传项 */
export function retainedAttachmentDrafts(
  attachments: readonly AttachmentWire[],
): AttachmentDraft[] {
  return attachments.map((item) => ({
    attachment_id: item.attachment_id,
    file_name: item.file_name,
    size_bytes: item.size_bytes,
    input: { kind: "reference", attachment_id: item.attachment_id },
  }));
}

/** 账本与草稿共用形状的双向转换：提交与显式重试沿用的完整请求输入 */
export function attachmentInputs(
  drafts: readonly AttachmentDraft[],
): AttachmentInputWire[] {
  return drafts.map((item) => item.input);
}

/** 账本读取：字段与请求输入均按契约严格校验，展示身份与请求输入身份必须一致 */
export function fromAttachmentDraft(value: unknown): AttachmentDraft {
  if (!isObject(value)) throw new Error("附件账本无效。");
  const input = parseAttachmentInputWire(value.input);
  const attachment_id = requireUuid(value.attachment_id, "attachment_id");
  if (input.attachment_id !== attachment_id)
    throw new Error("附件账本身份不一致。");
  return {
    attachment_id,
    file_name: requireAttachmentName(value.file_name, "文件名"),
    size_bytes: requireNonNegativeInt(value.size_bytes, "size_bytes"),
    input,
  };
}

/** 首次发送的会话标题（§3.1）：文本含非空白内容时沿用原文，纯文件消息按附件顺序以换行连接文件名 */
export function draftSessionTitle(
  request: string,
  drafts: readonly AttachmentDraft[],
): string {
  if (request.trim() !== "") return request;
  return drafts.map((item) => item.file_name).join("\n");
}
