<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const rejects = ref([])
const selected = ref(null)
const detail = ref(null)
const gate = ref(null)

async function load() {
  ;[proofs.value, rejects.value] = await Promise.all([api.proofs(), api.rejects()])
}
async function inspect(row) {
  selected.value = row.occupancy_id
  const [d, g] = await Promise.all([
    api.rejectProof(row.occupancy_id),
    api.gateProof(row.occupancy_id).catch(() => null),
  ])
  detail.value = d
  gate.value = g
}
onMounted(load)
</script>
<template>
  <div data-plant="27-released-write-badge-stale" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">占用核对只读；已释放占用的追加事件一律进搁置，历史行不变。</p>

    <div class="panel" style="overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用核对</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">已释放占用追加拒收</h2>
      <p v-if="!rejects.length" class="sub">暂无拒收记录。</p>
      <table v-else>
        <thead>
          <tr><th>id</th><th>占用</th><th>事件</th><th>占用态</th><th>时刻</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rejects" :key="r.id">
            <td>{{ r.id }}</td>
            <td><code>{{ r.occupancy_id }}</code></td>
            <td><code>{{ r.event_id }}</code></td>
            <td>{{ r.occupancy_status }}</td>
            <td>{{ r.created_at }}</td>
            <td><button @click="inspect(r)">核对</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="detail" class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">
        占用 {{ detail.occupancy_id }}
        <small class="sub" style="margin:0">{{ detail.immutable ? '历史行不可变' : '非已释放' }}</small>
      </h2>
      <table>
        <thead><tr><th>状态</th><th>开启</th><th>关闭</th><th>最近事件</th></tr></thead>
        <tbody>
          <tr>
            <td>{{ detail.occupancy?.status }}</td>
            <td>{{ detail.occupancy?.opened_at }}</td>
            <td>{{ detail.occupancy?.closed_at }}</td>
            <td><code>{{ detail.occupancy?.last_event_id }}</code></td>
          </tr>
        </tbody>
      </table>
      <h3 style="font-size:.95rem;margin:1rem 0 .4rem">搁置原始载荷（共 {{ detail.rejected_count }} 条）</h3>
      <pre v-for="x in detail.rejections" :key="x.id" class="panel" style="margin:.4rem 0">{{ x.member_json }}</pre>

      <template v-if="gate">
        <h3 style="font-size:.95rem;margin:1rem 0 .4rem">
          写闸角标
          <small class="sub" style="margin:0">
            {{ gate.gate.writable ? '仍可写' : '已卸' }} · rev {{ gate.gate.gate_rev }} ·
            {{ gate.consistent ? '核证一致' : '核证异常：' + gate.problems.join('、') }}
          </small>
        </h3>
        <table>
          <thead><tr><th>可写</th><th>卸标时刻</th><th>卸标事件</th><th>原因</th><th>审计行</th></tr></thead>
          <tbody>
            <tr>
              <td>{{ gate.gate.writable ? '是' : '否' }}</td>
              <td>{{ gate.gate.released_at || '—' }}</td>
              <td><code>{{ gate.gate.released_by_event_id || '—' }}</code></td>
              <td>{{ gate.gate.release_reason || '—' }}</td>
              <td>{{ gate.audit_count }} / 缺 {{ gate.missing_audits.length }}</td>
            </tr>
          </tbody>
        </table>
        <table style="margin-top:.5rem">
          <thead><tr><th>审计 id</th><th>事件</th><th>动作</th><th>gate_rev</th><th>时刻</th></tr></thead>
          <tbody>
            <tr v-for="a in gate.audits" :key="a.id">
              <td>{{ a.id }}</td>
              <td><code>{{ a.event_id || '—' }}</code></td>
              <td>{{ a.action }}</td>
              <td>{{ a.gate_rev }}</td>
              <td>{{ a.created_at }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>
  </div>
</template>
