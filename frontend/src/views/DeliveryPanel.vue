<template>
  <section class="delivery-panel" data-testid="delivery-panel">
    <header class="delivery-head">
      <div>
        <h3>可恢复交付记录</h3>
        <p class="page-desc">
          位点确认进度与可下载成品分开核对：提交成功但文件未生成时，旧成品保持不变，待办范围可重试。
        </p>
      </div>
      <form class="delivery-form" @submit.prevent="openManifest">
        <label>
          <span>日志源编号</span>
          <input v-model="sourceKey" :data-testid="`delivery-source`" placeholder="例如 LOG_-0003" />
        </label>
        <button class="btn" data-testid="delivery-open" type="submit">打开导出清单</button>
      </form>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="delivery-error">{{ errorMessage }}</p>

    <template v-if="manifest">
      <div class="delivery-summary" data-testid="delivery-summary" :data-status="manifest.status">
        <div>
          <span class="stat-label">位点行号</span>
          <strong class="stat-value">{{ manifest.checkpoint_line }}</strong>
        </div>
        <div>
          <span class="stat-label">已确认成品到</span>
          <strong class="stat-value">{{ manifest.artifact?.line_to ?? manifest.confirmed_line }}</strong>
        </div>
        <div>
          <span class="stat-label">待办范围</span>
          <strong class="stat-value">
            {{ manifest.pending_range ? `${manifest.pending_range.from_line}-${manifest.pending_range.to_line}` : '无' }}
          </strong>
        </div>
        <div>
          <span class="stat-label">清单状态</span>
          <strong class="stat-value">{{ statusText(manifest.status) }}</strong>
        </div>
      </div>

      <p class="delivery-message" data-testid="delivery-message">{{ manifest.message }}</p>
      <p v-if="manifest.download_is_stale" class="delivery-warning" data-testid="delivery-stale">
        当前可下载的只是第 {{ manifest.artifact?.line_to }} 行成品，位点已到第 {{ manifest.checkpoint_line }} 行。
      </p>

      <div class="page-actions">
        <button
          class="btn"
          data-testid="delivery-commit"
          type="button"
          :disabled="busy"
          @click="commit"
        >
          提交当前位点
        </button>
        <button
          class="btn"
          data-testid="delivery-retry"
          type="button"
          :disabled="busy || !manifest.can_retry"
          @click="retry"
        >
          重试生成未完成成品
        </button>
        <button
          class="btn primary"
          data-testid="delivery-download"
          type="button"
          :disabled="busy"
          @click="download(false)"
        >
          下载已确认成品
        </button>
        <button
          class="btn ghost"
          data-testid="delivery-download-confirmed"
          type="button"
          :disabled="busy || !manifest.artifact?.line_to"
          @click="downloadConfirmedOnly"
        >
          下载到第 {{ manifest.artifact?.line_to ?? 0 }} 行
        </button>
      </div>

      <h4>交付记录</h4>
      <table class="data-table" data-testid="delivery-records">
        <thead>
          <tr><th>交付编号</th><th>新增行段</th><th>格式</th><th>状态</th><th>尝试</th><th>确认次数</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in manifest.deliveries" :key="record.delivery_id">
            <td>{{ record.delivery_id }}</td>
            <td>{{ record.from_line }}-{{ record.to_line }}</td>
            <td>{{ record.format }}</td>
            <td>{{ deliveryText(record.status) }}</td>
            <td>{{ record.attempts ?? 0 }}</td>
            <td>{{ record.confirmation_count ?? 0 }}</td>
            <td>{{ record.error || record.failure_reason || '—' }}</td>
          </tr>
          <tr v-if="!manifest.deliveries.length">
            <td colspan="7" class="empty-state">尚无交付记录</td>
          </tr>
        </tbody>
      </table>

      <h4>下载记录</h4>
      <table class="data-table" data-testid="delivery-downloads">
        <thead>
          <tr><th>下载编号</th><th>请求行号</th><th>成品范围</th><th>状态</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in manifest.downloads" :key="item.download_id">
            <td>{{ item.download_id }}</td>
            <td>{{ item.requested_line }}</td>
            <td>{{ item.artifact_line_from ?? '—' }}-{{ item.artifact_line_to ?? '—' }}</td>
            <td>{{ downloadText(item.status) }}</td>
            <td>{{ formatTime(item.at) }}</td>
          </tr>
          <tr v-if="!manifest.downloads.length">
            <td colspan="5" class="empty-state">尚无下载记录</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import {
  commitDelivery,
  downloadDelivery,
  loadDeliveryManifest,
  retryDelivery,
  saveBlob,
} from '@/api/delivery-service'
import type { DeliveryManifest } from '@/data/types'

const props = defineProps<{ defaultSource?: string }>()

const sourceKey = ref(props.defaultSource ?? 'LOG_-0003')
const manifest = ref<DeliveryManifest | null>(null)
const errorMessage = ref('')
const busy = ref(false)

const STATUS_TEXT: Record<string, string> = {
  confirmed: '已确认',
  partially_committed: '位点已提交，成品未完成',
  pending_delivery: '待生成成品',
  confirmed_behind_checkpoint: '成品落后于位点',
  no_delivery: '尚无成品',
}

const DELIVERY_TEXT: Record<string, string> = {
  pending: '待生成',
  generating: '生成中',
  failed: '生成失败，可重试',
  delivered: '已确认',
  superseded: '已被新成品取代',
  abandoned: '已放弃',
}

const DOWNLOAD_TEXT: Record<string, string> = {
  served: '已下载',
  stale: '成品未覆盖请求行',
  stale_allowed: '显式下载旧成品',
  not_ready: '成品未就绪',
}

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status
}

function deliveryText(status: string): string {
  return DELIVERY_TEXT[status] ?? status
}

function downloadText(status: string): string {
  return DOWNLOAD_TEXT[status] ?? status
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

async function reload() {
  if (!sourceKey.value.trim()) return
  errorMessage.value = ''
  try {
    manifest.value = await loadDeliveryManifest(sourceKey.value.trim())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付清单读取失败'
  }
}

async function openManifest() {
  await reload()
}

async function retry() {
  if (!manifest.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    await retryDelivery(manifest.value.source_key)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  } finally {
    busy.value = false
  }
}

async function download(allowStale: boolean) {
  if (!manifest.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const outcome = await downloadDelivery(
      manifest.value.source_key,
      manifest.value.checkpoint_line,
      { allowStale },
    )
    if (!outcome.ok) {
      errorMessage.value = outcome.message
    } else {
      saveBlob(outcome.blob, outcome.fileName)
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载失败'
  } finally {
    busy.value = false
  }
}

async function downloadConfirmedOnly() {
  if (!manifest.value?.artifact?.line_to) return
  busy.value = true
  errorMessage.value = ''
  try {
    const outcome = await downloadDelivery(
      manifest.value.source_key,
      manifest.value.artifact.line_to,
    )
    if (!outcome.ok) errorMessage.value = outcome.message
    else saveBlob(outcome.blob, outcome.fileName)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载失败'
  } finally {
    busy.value = false
  }
}

async function commit() {
  if (!sourceKey.value.trim()) return
  busy.value = true
  errorMessage.value = ''
  try {
    await commitDelivery(sourceKey.value.trim())
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交失败'
  } finally {
    busy.value = false
  }
}

watch(
  () => props.defaultSource,
  (value) => {
    if (value && value !== sourceKey.value) {
      sourceKey.value = value
      void reload()
    }
  },
)

watch(sourceKey, () => {
  manifest.value = null
})

onMounted(reload)

defineExpose({ commit, reload })
</script>
