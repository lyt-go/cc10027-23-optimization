/** 可恢复交付接口：清单、提交、重试与下载。 */
import { request } from './client'
import type { DeliveryManifest } from '@/data/types'

export async function loadDeliveryManifest(sourceKey: string): Promise<DeliveryManifest> {
  const response = await request(`/api/delivery/${encodeURIComponent(sourceKey)}/manifest`)
  if (!response.ok) throw new Error('交付清单读取失败')
  return (await response.json()) as DeliveryManifest
}

export async function commitDelivery(sourceKey: string, format = 'jsonl'): Promise<Record<string, unknown>> {
  const response = await request(
    `/api/delivery/${encodeURIComponent(sourceKey)}/commit?format_type=${encodeURIComponent(format)}`,
    { method: 'POST' },
  )
  return (await response.json()) as Record<string, unknown>
}

export async function retryDelivery(sourceKey: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/delivery/${encodeURIComponent(sourceKey)}/retry`, {
    method: 'POST',
  })
  return (await response.json()) as Record<string, unknown>
}

export type DownloadOutcome =
  | { ok: true; blob: Blob; fileName: string; lineFrom: string; lineTo: string; stale: boolean }
  | { ok: false; status: string; message: string; pendingRange: { from_line: number; to_line: number } | null }

export async function downloadDelivery(
  sourceKey: string,
  line: number,
  { allowStale = false }: { allowStale?: boolean } = {},
): Promise<DownloadOutcome> {
  const query = new URLSearchParams({ line: String(line) })
  if (allowStale) query.set('allow_stale', 'true')
  const response = await request(
    `/api/delivery/${encodeURIComponent(sourceKey)}/download?${query.toString()}`,
  )
  if (response.status === 409) {
    const payload = await response.json()
    return {
      ok: false,
      status: String(payload.status ?? 'not_ready'),
      message: String(payload.message ?? '成品尚未准备好'),
      pendingRange: payload.pending_range ?? null,
    }
  }
  if (!response.ok) throw new Error(`成品下载失败：${response.status}`)
  return {
    ok: true,
    blob: await response.blob(),
    fileName: `${sourceKey}_第${response.headers.get('X-Artifact-Line-To') ?? line}行.jsonl`,
    lineFrom: response.headers.get('X-Artifact-Line-From') ?? '0',
    lineTo: response.headers.get('X-Artifact-Line-To') ?? '0',
    stale: response.headers.get('X-Artifact-Stale') === 'true',
  }
}

export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
