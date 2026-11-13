<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getJSON, postJSON } from '../api'

const pairs = ref([])
const batches = ref([])
const pairId = ref(null)
const batchId = ref(null)
const diffs = ref([])
const records = ref([])
const events = ref([])
const stats = ref(null)
const sideValues = ref([])
const sideTab = ref('right')
const rollbackBatches = ref([])
const directions = ref({})
const selected = ref(new Set())
const lastRollback = ref(null)
const msg = ref('')
const err = ref('')
const busy = ref(false)

const canon = (k) => JSON.stringify(Object.fromEntries(Object.entries(k || {}).sort()))
const fmtErr = (e) => {
  const t = String(e.message || e)
  try { return JSON.parse(t).detail || t } catch { return t }
}

const kindLabel = { left_only: '仅左有', right_only: '仅右有', field_mismatch: '字段不一致' }
const statusMeta = {
  applied: { label: '盖写中', cls: 'ok' },
  rolling_back: { label: '回滚中', cls: 'warn' },
  rolled_back: { label: '已回滚', cls: '' },
  rollback_failed: { label: '回滚失败', cls: 'danger' },
}
const eventLabel = {
  apply: '盖写', apply_blocked: '盖写被拒', rollback_start: '回滚开始',
  rollback_success: '回滚成功', rollback_failed: '回滚失败',
  rollback_rejected: '回滚被拒', dirty_cleared: '重对清脏',
}
const batchStatusMeta = {
  running: { label: '进行中', cls: 'warn' }, completed: { label: '全部成功', cls: 'ok' },
  partial: { label: '部分成功', cls: 'warn' }, failed: { label: '整批失败', cls: 'danger' },
}

const pairBatches = computed(() => batches.value.filter((b) => b.pair_id === pairId.value))
const rollingBackKeys = computed(() =>
  new Set(records.value.filter((r) => r.status === 'rolling_back').map((r) => canon(r.align_key)))
)
const latestAppliedByKey = computed(() => {
  const m = {}
  for (const r of records.value) {
    if (r.status !== 'applied') continue
    const k = canon(r.align_key)
    if (!m[k] || r.id > m[k]) m[k] = r.id
  }
  return m
})

async function refresh() {
  const q = pairId.value ? `?pair_id=${pairId.value}` : ''
  ;[records.value, events.value, stats.value, rollbackBatches.value] = await Promise.all([
    getJSON(`/api/apply/records${q}`),
    getJSON(`/api/apply/events${q}${q ? '&' : '?'}limit=100`),
    getJSON(`/api/apply/stats${q}`),
    getJSON('/api/apply/rollback-batches'),
  ])
  if (pairId.value) sideValues.value = await getJSON(`/api/apply/side-values?pair_id=${pairId.value}`)
}

async function loadDiffs() {
  diffs.value = batchId.value ? await getJSON(`/api/diffs?batch_id=${batchId.value}`) : []
  for (const d of diffs.value) {
    if (!(d.id in directions.value)) directions.value[d.id] = 'left_to_right'
  }
}

async function applyDiff(d) {
  err.value = ''; msg.value = ''; busy.value = true
  try {
    const rec = await postJSON('/api/apply', { diff_id: d.id, direction: directions.value[d.id] })
    msg.value = `盖写成功：记录 #${rec.id}，旧值已留痕，可回滚。`
    await refresh()
  } catch (e) { err.value = fmtErr(e); await refresh() } finally { busy.value = false }
}

async function rollback(ids) {
  err.value = ''; msg.value = ''; busy.value = true
  try {
    const out = await postJSON('/api/apply/rollback', { apply_ids: ids })
    lastRollback.value = out
    const b = out.batch
    msg.value = `回滚批次 ${b.batch_no}：${batchStatusMeta[b.status].label}（成功 ${b.succeeded} / 失败 ${b.failed}）`
    selected.value = new Set()
    await refresh()
  } catch (e) { err.value = fmtErr(e); await refresh() } finally { busy.value = false }
}

function toggleSelect(id) {
  const s = new Set(selected.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selected.value = s
}

watch(pairId, async () => {
  batchId.value = pairBatches.value.length ? pairBatches.value[0].id : null
  await loadDiffs(); await refresh()
})
watch(batchId, loadDiffs)

onMounted(async () => {
  try {
    ;[pairs.value, batches.value] = await Promise.all([
      getJSON('/api/pairs'), getJSON('/api/batches?limit=50'),
    ])
    pairId.value = pairs.value.length ? pairs.value[0].id : null
    if (!pairId.value) await refresh()
  } catch (e) { err.value = fmtErr(e) }
})
</script>

<template>
  <div class="page">
    <h1>差异处置 · 盖写与回滚</h1>
    <p class="muted">
      可审计盖写：盖写前必留旧值快照，无旧值禁止盖写；回滚还原旧值并标脏待重对（重对才清脏）。
      回滚进行中同一键禁止再盖写。批量回滚策略：<b>部分成功</b>——已回滚键保持结果、失败键留痕、批次记部分成功。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="msg" class="tag ok">{{ msg }}</p>

    <div class="panel" v-if="stats">
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ stats.applies }}</div><div class="stat-label">盖写数</div></div>
        <div class="stat"><div class="stat-num">{{ stats.rolled_back }}</div><div class="stat-label">回滚成功</div></div>
        <div class="stat"><div class="stat-num">{{ stats.rollback_failed }}</div><div class="stat-label">回滚失败</div></div>
        <div class="stat"><div class="stat-num">{{ stats.dirty_keys }}</div><div class="stat-label">仍脏键</div></div>
        <div class="stat"><div class="stat-num">{{ stats.rollback_rejected }}</div><div class="stat-label">回滚拒绝（留痕）</div></div>
      </div>
      <p style="margin:0.6rem 0 0">
        计数与留痕、侧数据核对：
        <span class="tag" :class="stats.consistency.consistent ? 'ok' : 'danger'">
          {{ stats.consistency.consistent ? '一致' : '不一致' }}
        </span>
      </p>
      <details v-if="!stats.consistency.consistent" style="margin-top:0.4rem">
        <summary>核对明细</summary>
        <ul><li v-for="c in stats.consistency.checks" :key="c.name">
          <span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '✓' : '✗' }}</span> {{ c.name }}：{{ c.detail }}
        </li></ul>
      </details>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">① 盖写（先留旧值，再盖写）</h3>
      <div style="display:flex;flex-wrap:wrap;gap:0.75rem;align-items:flex-end;margin-bottom:0.6rem">
        <label>对账对
          <select v-model.number="pairId" style="display:block;min-width:16rem;margin-top:0.25rem">
            <option v-for="p in pairs" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
        </label>
        <label>差异来源批次
          <select v-model.number="batchId" style="display:block;min-width:14rem;margin-top:0.25rem">
            <option v-for="b in pairBatches" :key="b.id" :value="b.id">#{{ b.id }} · {{ b.batch_no }}</option>
          </select>
        </label>
        <span class="muted" v-if="!pairBatches.length">该对账对还没有批次，先去<router-link to="/run">对账台</router-link>跑一趟。</span>
      </div>
      <table>
        <thead><tr><th>类型</th><th>对齐键</th><th>不一致字段</th><th>方向</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="d in diffs" :key="d.id">
            <td><span class="tag" :class="d.kind === 'field_mismatch' ? 'danger' : 'warn'">{{ kindLabel[d.kind] }}</span></td>
            <td class="mono">{{ JSON.stringify(d.align_key) }}</td>
            <td class="mono">{{ (d.mismatched_fields || []).join(', ') || '—' }}</td>
            <td>
              <select v-if="d.kind === 'field_mismatch'" v-model="directions[d.id]">
                <option value="left_to_right">左 → 右</option>
                <option value="right_to_left">右 → 左</option>
              </select>
              <span v-else class="muted">—</span>
            </td>
            <td>
              <button v-if="d.kind === 'field_mismatch'" :disabled="busy || rollingBackKeys.has(canon(d.align_key))"
                :title="rollingBackKeys.has(canon(d.align_key)) ? '该键回滚进行中，禁止再次盖写' : '留旧值快照后盖写'"
                @click="applyDiff(d)">盖写</button>
              <span v-if="d.kind === 'field_mismatch' && rollingBackKeys.has(canon(d.align_key))" class="tag warn">回滚中·禁盖写</span>
              <span v-if="d.kind !== 'field_mismatch'" class="tag warn" title="单侧差异：目标端无此行，无旧值可留痕">无旧值·禁盖写</span>
            </td>
          </tr>
          <tr v-if="!diffs.length"><td colspan="5" class="muted">本批无差异或未选批次。</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">② 盖写记录与回滚</h3>
      <div style="display:flex;gap:0.6rem;align-items:center;margin-bottom:0.6rem">
        <button class="secondary" :disabled="busy || !selected.size" @click="rollback([...selected])">
          批量回滚选中（{{ selected.size }}）
        </button>
        <span class="muted">逐键独立提交：已回滚键保持结果，失败键留痕，批次记部分成功。选入非「盖写中」记录将被拒并留痕。</span>
      </div>
      <table>
        <thead><tr><th></th><th>ID</th><th>对齐键</th><th>方向</th><th>旧值 → 新值</th><th>状态</th><th>脏</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in records" :key="r.id">
            <td><input type="checkbox" :checked="selected.has(r.id)" :disabled="r.status === 'rolling_back'"
              :title="r.status === 'rolling_back' ? '回滚进行中，禁止操作' : ''" @change="toggleSelect(r.id)" /></td>
            <td>{{ r.id }}</td>
            <td class="mono">{{ JSON.stringify(r.align_key) }}</td>
            <td>{{ r.direction === 'left_to_right' ? '左→右' : '右→左' }}</td>
            <td class="mono" style="max-width:22rem;word-break:break-all">
              {{ JSON.stringify(r.old_values) }} → {{ JSON.stringify(r.new_values) }}
            </td>
            <td>
              <span class="tag" :class="statusMeta[r.status].cls">{{ statusMeta[r.status].label }}</span>
              <span v-if="r.status === 'applied' && latestAppliedByKey[canon(r.align_key)] !== r.id" class="tag warn" title="该键有更新的盖写，须先回滚最新一笔">非最新</span>
            </td>
            <td><span v-if="r.dirty" class="tag warn" title="已变更，待重对清脏">脏·待重对</span><span v-else class="muted">—</span></td>
            <td>
              <button v-if="r.status === 'applied'" :disabled="busy" @click="rollback([r.id])">回滚</button>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!records.length"><td colspan="8" class="muted">暂无盖写记录。</td></tr>
        </tbody>
      </table>
      <div v-if="lastRollback" style="margin-top:0.6rem">
        <b>最近回滚批次 {{ lastRollback.batch.batch_no }}</b>
        <span class="tag" :class="batchStatusMeta[lastRollback.batch.status].cls" style="margin-left:0.4rem">
          {{ batchStatusMeta[lastRollback.batch.status].label }}
        </span>
        <ul style="margin:0.4rem 0 0">
          <li v-for="r in lastRollback.results" :key="r.apply_id">
            记录 #{{ r.apply_id }} <span class="mono">{{ JSON.stringify(r.align_key) }}</span>：
            <span :class="['tag', r.ok ? 'ok' : 'danger']">{{ r.ok ? '已回滚' : `失败 · ${r.reason}` }}</span>
          </li>
        </ul>
      </div>
      <table v-if="rollbackBatches.length" style="margin-top:0.6rem">
        <thead><tr><th>回滚批次</th><th>策略</th><th>状态</th><th>成功/失败/总数</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="b in rollbackBatches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td class="mono">{{ b.strategy }}</td>
            <td><span class="tag" :class="batchStatusMeta[b.status].cls">{{ batchStatusMeta[b.status].label }}</span></td>
            <td>{{ b.succeeded }} / {{ b.failed }} / {{ b.total }}</td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">③ 留痕（审计事件）</h3>
      <table>
        <thead><tr><th>时间</th><th>事件</th><th>记录</th><th>对齐键</th><th>详情</th></tr></thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td class="mono">{{ e.created_at }}</td>
            <td><span class="tag" :class="{ danger: ['rollback_failed', 'apply_blocked'].includes(e.event), warn: ['rollback_rejected', 'rollback_start'].includes(e.event), ok: ['apply', 'rollback_success', 'dirty_cleared'].includes(e.event) }">{{ eventLabel[e.event] || e.event }}</span></td>
            <td>{{ e.apply_id ?? '—' }}</td>
            <td class="mono">{{ e.align_key ? JSON.stringify(e.align_key) : '—' }}</td>
            <td class="mono" style="max-width:26rem;word-break:break-all">{{ e.detail ? JSON.stringify(e.detail) : '—' }}</td>
          </tr>
          <tr v-if="!events.length"><td colspan="5" class="muted">暂无留痕。</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">④ 侧数据（当前有效值）</h3>
      <p class="muted" style="margin-top:0">
        盖写/回滚直接改写此层；对账按「夹具 + 侧数据」重算。脏键去<router-link to="/run">对账台</router-link>重对一趟后清脏。
      </p>
      <div style="margin-bottom:0.5rem">
        <button class="secondary" :disabled="sideTab === 'left'" @click="sideTab = 'left'">左端</button>
        <button class="secondary" :disabled="sideTab === 'right'" @click="sideTab = 'right'" style="margin-left:0.4rem">右端</button>
      </div>
      <table>
        <thead><tr><th>对齐键</th><th>当前值</th><th>更新时间</th></tr></thead>
        <tbody>
          <tr v-for="sv in sideValues.filter((s) => s.side === sideTab)" :key="sv.id">
            <td class="mono">{{ JSON.stringify(sv.align_key) }}</td>
            <td class="mono" style="max-width:30rem;word-break:break-all">{{ JSON.stringify(sv.values) }}</td>
            <td class="mono">{{ sv.updated_at }}</td>
          </tr>
          <tr v-if="!sideValues.filter((s) => s.side === sideTab).length"><td colspan="3" class="muted">该端暂无侧数据（未被盖写过的键仍取夹具原值）。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
