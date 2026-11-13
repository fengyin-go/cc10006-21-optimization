<template>
  <section class="page" data-module="search">
    <header class="page-head">
      <div>
        <h2>数据检索管理</h2>
        <p class="page-desc">
          索引影响视图：并列展示覆盖表名、索引字段与排序权重，查询后可直接看到哪张表命中、为什么排在前面。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openWeightDialog()">调整排序权重</button>
        <button class="btn" type="button" @click="refreshIndex()">刷新索引</button>
        <button class="btn" type="button" @click="exportRows">导出数据检索清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>检索关键词</span>
        <input v-model="filters.keyword" placeholder="例如：客户编号、order_silver" />
      </label>
      <label class="filter-item">
        <span>覆盖表名</span>
        <input v-model="filters.table" placeholder="按覆盖表名筛选" />
      </label>
      <label class="filter-item">
        <span>索引字段</span>
        <input v-model="filters.field" placeholder="按索引字段筛选" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p class="view-summary">
      当前视图：覆盖 <strong>{{ hitSummary.覆盖表数 }}</strong> 张表，命中
      <strong>{{ hitSummary.命中总数 }}</strong> 个索引，生效元数据版本
      <strong>{{ hitSummary.元数据版本 ?? '—' }}</strong>，命中口径与列表、概览一致
      <span v-if="hitSummary.筛选口径?.关键词 && hitSummary.筛选口径.关键词 !== '全部'">，关键词「{{ hitSummary.筛选口径.关键词 }}」</span>
    </p>

    <!-- 影响概览：覆盖表名、索引字段、排序权重并排呈现 -->
    <section class="impact-grid" data-view="impact-overview">
      <article v-for="hit in hitSummary.命中列表" :key="hit.索引编号" class="impact-card">
        <header class="impact-head">
          <div>
            <strong>{{ hit.索引名称 }}</strong>
            <span class="impact-code">{{ hit.索引编号 }}</span>
          </div>
          <span class="impact-score" :title="`命中分：${hit.命中分}`">命中分 {{ hit.命中分 }}</span>
        </header>
        <dl class="impact-meta">
          <div>
            <dt>覆盖表名</dt>
            <dd>{{ hit.覆盖表名 }}</dd>
          </div>
          <div>
            <dt>索引字段</dt>
            <dd>
              <span
                v-for="field in hit.索引字段"
                :key="field"
                class="chip"
                :class="{ 'chip-hit': hit.命中字段.includes(field) }"
              >{{ field }}</span>
            </dd>
          </div>
          <div>
            <dt>排序权重</dt>
            <dd>
              <span
                v-for="group in hit.权重明细"
                :key="group.字段分类"
                class="weight-pill"
                :class="`weight-${weightOf(hit, group.字段分类)}`"
                :title="`${group.字段分类}：${group.权重说明}`"
              >{{ group.字段分类 }} · {{ group.权重 }}</span>
            </dd>
          </div>
        </dl>
        <p class="impact-reason">{{ hit.权重理由 }}</p>
        <p class="impact-sentence">{{ hit.命中说明 }}</p>
        <footer class="impact-links">
          <button
            v-for="kind in objectKinds"
            :key="kind"
            class="link"
            type="button"
            :disabled="!(hit.关联对象?.[kind] || []).length"
            @click="jumpTo(kind, hit)"
          >{{ kind }}（{{ (hit.关联对象?.[kind] || []).length }}）</button>
          <button class="link" type="button" @click="openWeightDialog(hit)">调整权重</button>
          <button class="link" type="button" @click="refreshIndex(hit)">刷新索引</button>
        </footer>
      </article>
      <p v-if="!hitSummary.命中列表.length" class="empty-state impact-empty">
        当前筛选条件没有命中任何索引，可放宽关键词或先为覆盖表登记索引
      </p>
    </section>

    <details class="trace-panel">
      <summary>排序与版本追溯（本次生效元数据版本：{{ hitSummary.元数据版本 ?? '—' }}）</summary>
      <ol class="rank-list">
        <li v-for="item in hitSummary.排序说明" :key="item.索引编号">
          <span class="rank-index">#{{ item.序号 }}</span>
          <span>{{ item.索引名称 }} · {{ item.覆盖表名 }}</span>
          <span class="rank-score">命中分 {{ item.命中分 }}</span>
          <span class="rank-desc">{{ item.命中说明 }}</span>
        </li>
      </ol>
      <p v-for="note in hitSummary.重复索引 || []" :key="note.被覆盖索引" class="dup-note">
        重复索引：{{ note.说明 }}
      </p>
      <table class="data-table" v-if="versionTrace.length">
        <thead><tr><th>元数据版本</th><th>记录时间</th><th>来源</th></tr></thead>
        <tbody>
          <tr v-for="(trace, index) in versionTrace" :key="`${trace.元数据版本}-${index}`">
            <td>{{ trace.元数据版本 }}</td>
            <td>{{ trace.记录时间 }}</td>
            <td>{{ trace.来源 }}</td>
          </tr>
        </tbody>
      </table>
    </details>

    <h3 class="section-title">索引列表</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >{{ action }}</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据检索数据，可先登记检索索引</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据检索记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="weightDialog.open" class="dialog-mask" @click.self="closeWeightDialog">
      <section class="dialog">
        <header>
          <h3>调整排序权重 · {{ weightDialog.indexName }}</h3>
          <button class="link" type="button" @click="closeWeightDialog">关闭</button>
        </header>
        <p class="page-desc">
          业务域字段设高、时间字段设中后，含对应字段的查询会按权重计分排在前面。
          非法取值会被拒绝，修改后旧排序立即清除。
        </p>
        <form @submit.prevent="submitWeights">
          <label v-for="group in weightGroups" :key="group" class="filter-item">
            <span>{{ group }}</span>
            <select v-model="weightDialog.weights[group]">
              <option v-for="level in weightLevels" :key="level" :value="level">{{ level }}</option>
            </select>
          </label>
          <footer class="dialog-foot">
            <span v-if="weightDialog.message" :class="weightDialog.ok ? 'ok-text' : 'error-text'">
              {{ weightDialog.message }}
            </span>
            <button class="btn primary" type="submit">保存权重并刷新</button>
          </footer>
        </form>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { request } from '@/api/client'

type WeightGroup = '业务域字段' | '时间字段' | '度量字段'

type Hit = {
  id?: number
  索引编号: string
  索引名称: string
  覆盖表名: string
  索引字段: string[]
  命中字段: string[]
  命中分类: string[]
  命中方式: string
  排序权重: Record<WeightGroup, string>
  权重明细: { 字段分类: string; 权重: string; 权重说明: string; 字段: string[] }[]
  权重理由: string
  命中分: number
  命中说明: string
  关联对象?: Record<string, { id: number; 编号: string; 名称: string; 跳转: string }[]>
  生效版本: number
  生效元数据版本: number
  排序版本: number
  刷新时间: string
  状态: string
}

type HitSummary = {
  命中总数: number
  覆盖表数: number
  命中列表: Hit[]
  排序说明: { 序号: number; 索引编号: string; 索引名称: string; 覆盖表名: string; 命中分: number; 命中说明: string }[]
  重复索引?: { 覆盖表名: string; 保留索引: string; 被覆盖索引: string; 说明: string }[]
  生效版本?: number
  元数据版本?: number
  索引刷新时间?: string
  筛选口径?: { 关键词: string; 命中索引: string }
}

type Row = Record<string, string | number | null | Record<string, unknown>>

const router = useRouter()
const ENDPOINT = '/api/search'
const columns = ['索引编号', '索引名称', '覆盖表名', '索引字段', '分词策略', '排序权重', '刷新频率', '索引大小']
const actions = ['提交构建', '启用索引', '标记失效']
const statuses = ['草稿', '待构建', '已生效', '已失效']
const objectKinds = ['目录', '血缘', '质量']
const weightGroups: WeightGroup[] = ['业务域字段', '时间字段', '度量字段']
const weightLevels = ['高', '中', '低', '关闭']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const versionTrace = ref<{ 元数据版本: number; 记录时间: string; 来源: string }[]>([])
const filters = reactive({ keyword: '', table: '', field: '', status: '' })

const emptySummary = (): HitSummary => ({
  命中总数: 0,
  覆盖表数: 0,
  命中列表: [],
  排序说明: [],
  重复索引: [],
  元数据版本: 0,
})
const hitSummary = ref<HitSummary>(emptySummary())
const stats = ref([
  { label: '检索索引总数', value: 0 },
  { label: '命中覆盖表数', value: 0 },
  { label: '已生效索引', value: 0 },
])

const weightDialog = reactive({
  open: false,
  id: 0,
  indexName: '',
  weights: { 业务域字段: '低', 时间字段: '低', 度量字段: '低' } as Record<WeightGroup, string>,
  message: '',
  ok: true,
})

function currentQuery(): string {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value)
  })
  return params.toString()
}

function weightOf(hit: Hit, group: string): string {
  const weights = hit.排序权重 as Record<string, string>
  return weights[group] || '低'
}

function formatCell(row: Row, column: string): string {
  const value = row[column]
  if (value == null || value === '') return '—'
  if (column === '排序权重' && typeof value === 'object') {
    return Object.entries(value as Record<string, string>)
      .map(([group, level]) => `${group}=${level}`)
      .join('，')
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function resetFilters() {
  filters.keyword = ''
  filters.table = ''
  filters.field = ''
  filters.status = ''
  void reload()
}

function exportRows() {
  // 导出沿用当前筛选，避免导出的清单和页面上的命中视图对不上
  window.open(`${ENDPOINT}/export?${currentQuery()}`, '_blank')
}

function jumpTo(kind: string, hit: Hit) {
  const target = hit.关联对象?.[kind]?.[0]
  if (target) void router.push(target.跳转)
}

function openWeightDialog(hit?: Hit) {
  if (!hit) {
    const first = hitSummary.value.命中列表[0]
    if (!first) {
      errorMessage.value = '当前没有可调整权重的索引，请先查询出命中索引'
      return
    }
    hit = first
  }
  weightDialog.open = true
  weightDialog.id = Number(hit.id ?? 0)
  weightDialog.indexName = `${hit.索引名称}（${hit.索引编号}）`
  weightDialog.weights = {
    业务域字段: hit.排序权重.业务域字段 ?? '低',
    时间字段: hit.排序权重.时间字段 ?? '低',
    度量字段: hit.排序权重.度量字段 ?? '低',
  }
  weightDialog.message = ''
  weightDialog.ok = true
}

function closeWeightDialog() {
  weightDialog.open = false
}

async function submitWeights() {
  weightDialog.message = ''
  try {
    const response = await request(`${ENDPOINT}/${weightDialog.id}/weights`, {
      method: 'POST',
      body: JSON.stringify({ values: { 排序权重: weightDialog.weights } }),
    })
    const result = await response.json()
    weightDialog.ok = Boolean(result.ok)
    weightDialog.message = result.message
    if (result.ok) {
      // 权重变了，视图与列表都要按新口径重算，不能沿用旧顺序
      await reload()
      weightDialog.message = `${result.message}（元数据版本 ${result.data?.生效元数据版本 ?? '—'}）`
    }
  } catch (error) {
    weightDialog.ok = false
    weightDialog.message = error instanceof Error ? error.message : '权重更新失败'
  }
}

async function refreshIndex(target?: Hit) {
  const first = target ?? hitSummary.value.命中列表[0]
  if (!first) {
    errorMessage.value = '请先查询出命中索引再刷新'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${first.id}/refresh`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const result = await response.json()
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '索引刷新失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const result = await response.json()
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据检索操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = currentQuery()
  try {
    const response = await request(`${ENDPOINT}${query ? `?${query}` : ''}`)
    if (!response.ok) {
      throw new Error('检索索引列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    hitSummary.value = payload.hitSummary ?? emptySummary()
    versionTrace.value = payload.metadataVersionTrace ?? []
    stats.value = [
      { label: '检索索引总数', value: payload.total ?? rows.value.length },
      { label: '命中覆盖表数', value: hitSummary.value.覆盖表数 },
      {
        label: '已生效索引',
        value: rows.value.filter((row) => row.status === '已生效').length,
      },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据检索列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.impact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.impact-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.impact-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.impact-code {
  margin-left: 8px;
  color: var(--muted);
  font-size: 12px;
}
.impact-score {
  background: #eef4ff;
  color: #1f6feb;
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
  white-space: nowrap;
}
.impact-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
  margin: 0;
}
.impact-meta dt {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 3px;
}
.impact-meta dd {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 13px;
}
.chip {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  color: #334155;
}
.chip-hit {
  border-color: #1f6feb;
  background: #eef4ff;
  color: #1f6feb;
  font-weight: 600;
}
.weight-pill {
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 12px;
  border: 1px solid var(--border);
}
.weight-高 { background: #fde8e8; border-color: #f2b8b5; color: #b42318; }
.weight-中 { background: #fff5e6; border-color: #f5d9a8; color: #a15c00; }
.weight-低 { background: #eef6ee; border-color: #c7e3c7; color: #2f6b2f; }
.weight-关闭 { background: #f1f3f5; color: var(--muted); }
.impact-reason {
  margin: 0;
  font-size: 12px;
  color: #334155;
  background: #f8fafc;
  border-left: 3px solid var(--brand);
  padding: 6px 8px;
  border-radius: 2px;
}
.impact-sentence {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}
.impact-links {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: auto;
}
.impact-links .link:disabled { color: #b6c2cf; cursor: not-allowed; }
.impact-empty { grid-column: 1 / -1; padding: 18px; background: #fff; border: 1px dashed var(--border); border-radius: 8px; }
.view-summary {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  color: #334155;
  margin: 0 0 10px;
}
.trace-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-size: 13px;
}
.trace-panel summary { cursor: pointer; color: var(--muted); }
.rank-list { padding-left: 18px; margin: 10px 0; display: flex; flex-direction: column; gap: 6px; }
.rank-list li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.rank-index { color: var(--brand); font-weight: 600; }
.rank-score { color: #b42318; }
.rank-desc { color: var(--muted); font-size: 12px; }
.dup-note { color: #a15c00; font-size: 12px; margin: 4px 0; }
.section-title { font-size: 14px; margin: 4px 0 8px; }
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.dialog {
  background: #fff;
  border-radius: 10px;
  padding: 16px 18px;
  width: min(440px, 92vw);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.dialog header { display: flex; justify-content: space-between; align-items: center; }
.dialog header h3 { font-size: 15px; margin: 0; }
.dialog-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 8px; }
.ok-text { color: #2f6b2f; }
</style>
