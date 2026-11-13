<template>
  <div class="reply-control-view">
    <v-card color="surface" variant="flat" class="iris-hero-card mb-3">
      <v-card-title class="d-flex align-center iris-section-title">
        <v-icon icon="mdi-robot" color="primary" class="mr-2" />
        主动回复
        <v-spacer />
        <v-btn
          color="primary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="loadManage"
        >
          刷新
        </v-btn>
      </v-card-title>
      <v-card-text class="pt-0">
        <div class="text-body-2 text-medium-emphasis">
          管理白名单群聊的主动回复行为。触发与主动发起参数请在「隐藏参数」页调整。
        </div>
      </v-card-text>
    </v-card>

    <v-card color="surface" variant="flat" class="iris-card">
      <div class="pa-4">
        <div class="d-flex align-center ga-3 mb-4 flex-wrap">
          <v-text-field
            v-model="newGroupId"
            label="输入群ID以添加到白名单"
            density="compact"
            variant="outlined"
            hide-details
            style="max-width: 280px"
            @keydown.enter="handleEnableGroup"
          />
          <v-btn
            color="primary"
            variant="tonal"
            prepend-icon="mdi-plus"
            :loading="manageActionLoading"
            :disabled="!newGroupId.trim()"
            @click="handleEnableGroup"
          >
            启用群聊
          </v-btn>
        </div>

        <v-progress-circular
          v-if="loading"
          indeterminate
          color="primary"
          size="48"
          class="d-block mx-auto my-8"
        />

        <div v-else-if="!whitelist.length" class="iris-empty-state">
          <v-icon icon="mdi-inbox-outline" size="48" />
          <div class="iris-empty-state__title">暂无已启用的群聊</div>
          <div class="iris-empty-state__desc">在上方输入群ID并点击「启用群聊」</div>
        </div>

        <div v-else class="table-wrapper">
          <v-table density="compact" class="iris-table">
            <thead>
              <tr>
                <th>群ID</th>
                <th>意愿</th>
                <th>锚点</th>
                <th>滚动24h发起</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in whitelist" :key="g.group_id">
                <td class="font-weight-medium">{{ g.group_id }}</td>
                <td>
                  <v-select
                    :model-value="g.willingness"
                    :items="willingnessItems"
                    density="compact"
                    variant="outlined"
                    hide-details
                    style="min-width: 84px"
                    @update:model-value="(v: string) => handleSetWillingness(g.group_id, v)"
                  />
                </td>
                <td style="max-width: 260px">
                  <div v-if="g.anchor_kind" class="d-flex flex-wrap ga-1 align-center">
                    <v-chip size="x-small" variant="tonal" color="info">{{ g.anchor_kind }}</v-chip>
                    <v-chip
                      v-for="u in g.anchor_users || []"
                      :key="u"
                      size="x-small"
                      variant="tonal"
                      color="primary"
                    >
                      {{ u }}
                    </v-chip>
                    <v-chip
                      v-for="k in g.anchor_keywords || []"
                      :key="k"
                      size="x-small"
                      variant="tonal"
                      color="warning"
                    >
                      {{ k }}
                    </v-chip>
                    <span v-if="g.anchor_reason" class="text-caption text-medium-emphasis">
                      {{ g.anchor_reason }}
                    </span>
                  </div>
                  <span v-else class="text-medium-emphasis">-</span>
                </td>
                <td>
                  {{ g.initiate_daily_count }}
                  <v-chip v-if="g.initiate_pending" size="x-small" variant="tonal" color="warning" class="ml-1">
                    等待接话
                  </v-chip>
                </td>
                <td>
                  <v-tooltip text="重置群状态" location="top">
                    <template #activator="{ props }">
                      <v-btn
                        icon="mdi-restore"
                        variant="text"
                        size="x-small"
                        v-bind="props"
                        @click="handleResetGroup(g.group_id)"
                      />
                    </template>
                  </v-tooltip>
                  <v-tooltip text="禁用该群主动回复" location="top">
                    <template #activator="{ props }">
                      <v-btn
                        icon="mdi-block-helper"
                        variant="text"
                        size="x-small"
                        color="error"
                        v-bind="props"
                        @click="handleDisableGroup(g.group_id)"
                      />
                    </template>
                  </v-tooltip>
                </td>
              </tr>
            </tbody>
          </v-table>
        </div>
      </div>
    </v-card>

    <v-dialog v-model="confirmDialog" max-width="400" class="iris-dialog">
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon icon="mdi-alert" color="warning" class="mr-2" />
          确认操作
        </v-card-title>
        <v-card-text>{{ confirmMessage }}</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmDialog = false">取消</v-btn>
          <v-btn color="error" variant="tonal" @click="confirmAction">确认</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar
      v-model="showSnackbar"
      :color="snackbarColor"
      :timeout="4000"
      location="top"
    >
      {{ snackbarText }}
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import {
  getReplyWhitelist,
  enableReplyGroup,
  disableReplyGroup,
  setReplyWillingness,
  resetReplyGroup,
  type ReplyWhitelistGroup
} from '@/api/reply'

const loading = ref(false)
const whitelist = ref<ReplyWhitelistGroup[]>([])
const newGroupId = ref('')
const manageActionLoading = ref(false)

const confirmDialog = ref(false)
const confirmMessage = ref('')
const confirmCallback = ref<(() => void) | null>(null)

const showSnackbar = ref(false)
const snackbarText = ref('')
const snackbarColor = ref('success')

const willingnessItems = [
  { title: '低', value: 'low' },
  { title: '中', value: 'medium' },
  { title: '高', value: 'high' }
]

const willingnessText = (level: string): string => {
  const map: Record<string, string> = { low: '低', medium: '中', high: '高' }
  return map[level] || level
}

const notify = (text: string, color: string = 'success') => {
  snackbarText.value = text
  snackbarColor.value = color
  showSnackbar.value = true
}

const showConfirm = (message: string, callback: () => void) => {
  confirmMessage.value = message
  confirmCallback.value = callback
  confirmDialog.value = true
}

const confirmAction = () => {
  confirmDialog.value = false
  if (confirmCallback.value) {
    confirmCallback.value()
    confirmCallback.value = null
  }
}

const loadManage = async () => {
  loading.value = true
  try {
    whitelist.value = await getReplyWhitelist()
  } catch (e: unknown) {
    notify((e as Error).message || '加载白名单失败', 'error')
  } finally {
    loading.value = false
  }
}

const handleEnableGroup = async () => {
  const gid = newGroupId.value.trim()
  if (!gid) return
  manageActionLoading.value = true
  try {
    await enableReplyGroup(gid)
    newGroupId.value = ''
    notify(`已启用群 ${gid}`)
    loadManage()
  } catch (e: unknown) {
    notify((e as Error).message || '启用失败', 'error')
  } finally {
    manageActionLoading.value = false
  }
}

const handleDisableGroup = (gid: string) => {
  showConfirm(`确定要禁用群 ${gid} 的主动回复吗？`, async () => {
    try {
      await disableReplyGroup(gid)
      notify(`已禁用群 ${gid}`)
      loadManage()
    } catch (e: unknown) {
      notify((e as Error).message || '禁用失败', 'error')
    }
  })
}

const handleResetGroup = (gid: string) => {
  showConfirm(`确定要重置群 ${gid} 的状态吗？`, async () => {
    try {
      await resetReplyGroup(gid)
      notify(`已重置群 ${gid}`)
      loadManage()
    } catch (e: unknown) {
      notify((e as Error).message || '重置失败', 'error')
    }
  })
}

const handleSetWillingness = async (gid: string, willingness: string) => {
  try {
    await setReplyWillingness(gid, willingness)
    notify(`群 ${gid} 意愿已调整为「${willingnessText(willingness)}」`)
  } catch (e: unknown) {
    notify((e as Error).message || '设置失败', 'error')
    loadManage()
  }
}

onMounted(() => {
  loadManage()
  window.addEventListener('iris:refresh', loadManage)
})

onUnmounted(() => {
  window.removeEventListener('iris:refresh', loadManage)
})
</script>

<style scoped>
.table-wrapper {
  overflow-x: auto;
}
</style>
