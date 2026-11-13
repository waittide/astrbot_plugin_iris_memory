<template>
  <div class="data-manage-view">
    <v-row>
      <v-col cols="12">
        <v-card color="surface" variant="flat" class="iris-hero-card">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-icon icon="mdi-swap-vertical" color="primary" class="mr-2" />
            数据导入导出
            <v-spacer />
          </v-card-title>
          <v-card-text>
            <v-alert type="info" variant="tonal" density="compact" class="mb-4">
              <div class="text-body-2">
                支持导出和导入 L1 消息缓冲、L2 记忆、L3 知识图谱与画像数据，以及全量备份与恢复。
                导出数据为 JSON 格式，导入时默认跳过已存在的记录。
              </div>
            </v-alert>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-2">
      <v-col cols="12" md="6" lg="3">
        <v-card color="surface" variant="flat" class="iris-card iris-card-hover h-100">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-avatar color="primary" variant="tonal" size="32" class="mr-3">
              <v-icon icon="mdi-lightning-bolt" size="18" />
            </v-avatar>
            L1 缓冲
          </v-card-title>
          <v-card-text>
            <div class="d-flex flex-column ga-2">
              <v-btn
                color="primary"
                variant="tonal"
                prepend-icon="mdi-download"
                :loading="exportingL1"
                @click="handleExportL1"
              >
                导出 L1 缓冲
              </v-btn>
              <v-btn
                color="secondary"
                variant="tonal"
                prepend-icon="mdi-upload"
                :loading="importingL1"
                @click="triggerImportL1"
              >
                导入 L1 缓冲
              </v-btn>
              <input
                ref="l1FileInput"
                type="file"
                accept=".json"
                style="display: none"
                @change="handleImportL1File"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card color="surface" variant="flat" class="iris-card iris-card-hover h-100">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-avatar color="secondary" variant="tonal" size="32" class="mr-3">
              <v-icon icon="mdi-database" size="18" />
            </v-avatar>
            L2 记忆
          </v-card-title>
          <v-card-text>
            <div class="d-flex flex-column ga-2">
              <v-btn
                color="primary"
                variant="tonal"
                prepend-icon="mdi-download"
                :loading="exportingL2"
                @click="handleExportL2"
              >
                导出 L2 记忆
              </v-btn>
              <v-btn
                color="secondary"
                variant="tonal"
                prepend-icon="mdi-upload"
                :loading="importingL2"
                @click="triggerImportL2"
              >
                导入 L2 记忆
              </v-btn>
              <input
                ref="l2FileInput"
                type="file"
                accept=".json"
                style="display: none"
                @change="handleImportL2File"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card color="surface" variant="flat" class="iris-card iris-card-hover h-100">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-avatar color="accent" variant="tonal" size="32" class="mr-3">
              <v-icon icon="mdi-graph" size="18" />
            </v-avatar>
            L3 知识图谱
          </v-card-title>
          <v-card-text>
            <div class="d-flex flex-column ga-2">
              <v-btn
                color="primary"
                variant="tonal"
                prepend-icon="mdi-download"
                :loading="exportingL3"
                @click="handleExportL3"
              >
                导出知识图谱
              </v-btn>
              <v-btn
                color="secondary"
                variant="tonal"
                prepend-icon="mdi-upload"
                :loading="importingL3"
                @click="triggerImportL3"
              >
                导入知识图谱
              </v-btn>
              <input
                ref="l3FileInput"
                type="file"
                accept=".json"
                style="display: none"
                @change="handleImportL3File"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card color="surface" variant="flat" class="iris-card iris-card-hover h-100">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-avatar color="info" variant="tonal" size="32" class="mr-3">
              <v-icon icon="mdi-account-group" size="18" />
            </v-avatar>
            画像数据
          </v-card-title>
          <v-card-text>
            <div class="d-flex flex-column ga-2">
              <v-btn
                color="primary"
                variant="tonal"
                prepend-icon="mdi-download"
                :loading="exportingProfile"
                @click="handleExportProfile"
              >
                导出画像
              </v-btn>
              <v-btn
                color="secondary"
                variant="tonal"
                prepend-icon="mdi-upload"
                :loading="importingProfile"
                @click="triggerImportProfile"
              >
                导入画像
              </v-btn>
              <input
                ref="profileFileInput"
                type="file"
                accept=".json"
                style="display: none"
                @change="handleImportProfileFile"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-2">
      <v-col cols="12">
        <v-card color="surface" variant="flat" class="iris-card">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-icon icon="mdi-backup-restore" color="warning" class="mr-2" />
            全量备份与恢复
          </v-card-title>
          <v-card-text>
            <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
              <div class="text-body-2">
                全量备份将导出 L1 缓冲、L2 记忆、L3 知识图谱与画像数据。恢复时已存在的记录默认跳过。
              </div>
            </v-alert>
            <div class="d-flex ga-3 flex-wrap">
              <v-btn
                color="warning"
                variant="tonal"
                prepend-icon="mdi-download"
                :loading="exportingAll"
                @click="handleExportAll"
              >
                全量备份
              </v-btn>
              <v-btn
                color="warning"
                variant="tonal"
                prepend-icon="mdi-upload"
                :loading="importingAll"
                @click="triggerImportAll"
              >
                全量恢复
              </v-btn>
              <input
                ref="allFileInput"
                type="file"
                accept=".json"
                style="display: none"
                @change="handleImportAllFile"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-4">
      <v-col cols="12">
        <v-card color="surface" variant="flat" class="iris-card">
          <v-card-title class="d-flex align-center iris-section-title">
            <v-icon icon="mdi-delete-sweep" color="error" class="mr-2" />
            数据清理
          </v-card-title>
          <v-card-text>
            <v-alert type="error" variant="tonal" density="compact" class="mb-4">
              <div class="text-body-2">
                以下操作不可逆，请谨慎使用。建议在操作前先导出备份。
              </div>
            </v-alert>

            <v-row>
              <v-col cols="12" md="6" lg="3">
                <v-card variant="outlined" class="iris-card pa-3">
                  <div class="text-subtitle-2 mb-2 font-weight-medium">L1 缓冲</div>
                  <div class="text-caption text-medium-emphasis mb-3">清空所有会话的短期消息缓冲</div>
                  <v-btn
                    color="error"
                    variant="tonal"
                    size="small"
                    block
                    prepend-icon="mdi-delete"
                    :loading="clearingL1"
                    @click="handleClearL1"
                  >
                    清空 L1 缓冲
                  </v-btn>
                </v-card>
              </v-col>

              <v-col cols="12" md="6" lg="3">
                <v-card variant="outlined" class="iris-card pa-3">
                  <div class="text-subtitle-2 mb-2 font-weight-medium">L2 记忆</div>
                  <div class="text-caption text-medium-emphasis mb-3">删除所有长期记忆数据</div>
                  <v-btn
                    color="error"
                    variant="tonal"
                    size="small"
                    block
                    prepend-icon="mdi-delete"
                    :loading="deletingL2"
                    @click="handleDeleteL2"
                  >
                    删除所有 L2 记忆
                  </v-btn>
                </v-card>
              </v-col>

              <v-col cols="12" md="6" lg="3">
                <v-card variant="outlined" class="iris-card pa-3">
                  <div class="text-subtitle-2 mb-2 font-weight-medium">L3 知识图谱</div>
                  <div class="text-caption text-medium-emphasis mb-3">删除或合并知识图谱节点</div>
                  <div class="d-flex flex-column ga-2">
                    <v-btn
                      color="error"
                      variant="tonal"
                      size="small"
                      block
                      prepend-icon="mdi-delete"
                      :loading="deletingL3"
                      @click="handleDeleteL3"
                    >
                      删除所有图谱
                    </v-btn>
                    <v-btn
                      color="warning"
                      variant="tonal"
                      size="small"
                      block
                      prepend-icon="mdi-merge"
                      :loading="mergingL3"
                      @click="handleMergeL3"
                    >
                      合并重复节点
                    </v-btn>
                  </div>
                </v-card>
              </v-col>

              <v-col cols="12" md="6" lg="3">
                <v-card variant="outlined" class="iris-card pa-3">
                  <div class="text-subtitle-2 mb-2 font-weight-medium">画像数据</div>
                  <div class="text-caption text-medium-emphasis mb-3">删除所有群聊和用户画像</div>
                  <v-btn
                    color="error"
                    variant="tonal"
                    size="small"
                    block
                    prepend-icon="mdi-delete"
                    :loading="deletingProfile"
                    @click="handleDeleteProfile"
                  >
                    删除所有画像
                  </v-btn>
                </v-card>
              </v-col>
            </v-row>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

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
import { ref } from 'vue'
import { exportL1Buffer, importL1Buffer, exportL2Memory, importL2Memory, exportL3KG, importL3KG, exportProfiles, importProfiles, exportAll, importAll } from '@/api/data'
import { clearL1Buffer, deleteL2Memory, deleteL3KG, mergeL3Duplicates, deleteProfile } from '@/api/manage'

const exportingL1 = ref(false)
const importingL1 = ref(false)
const exportingL2 = ref(false)
const importingL2 = ref(false)
const exportingL3 = ref(false)
const importingL3 = ref(false)
const exportingProfile = ref(false)
const importingProfile = ref(false)
const exportingAll = ref(false)
const importingAll = ref(false)

const clearingL1 = ref(false)
const deletingL2 = ref(false)
const deletingL3 = ref(false)
const mergingL3 = ref(false)
const deletingProfile = ref(false)

const l1FileInput = ref<HTMLInputElement | null>(null)
const l2FileInput = ref<HTMLInputElement | null>(null)
const l3FileInput = ref<HTMLInputElement | null>(null)
const profileFileInput = ref<HTMLInputElement | null>(null)
const allFileInput = ref<HTMLInputElement | null>(null)

const confirmDialog = ref(false)
const confirmMessage = ref('')
const confirmCallback = ref<(() => void) | null>(null)

const showSnackbar = ref(false)
const snackbarText = ref('')
const snackbarColor = ref('success')

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

const notify = (text: string, color: string = 'success') => {
  snackbarText.value = text
  snackbarColor.value = color
  showSnackbar.value = true
}

const handleExportL1 = async () => {
  exportingL1.value = true
  try {
    await exportL1Buffer()
    notify('L1 缓冲导出成功')
  } catch (e: unknown) {
    notify((e as Error).message || '导出失败', 'error')
  } finally {
    exportingL1.value = false
  }
}

const triggerImportL1 = () => {
  l1FileInput.value?.click()
}

const handleImportL1File = async (event: Event) => {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  importingL1.value = true
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    const stats = await importL1Buffer(data)
    notify(`L1 缓冲导入成功：导入 ${stats.imported_messages} 条消息（${stats.imported_queues} 个会话），跳过 ${stats.skipped_messages} 条`)
  } catch (e: unknown) {
    notify((e as Error).message || '导入失败', 'error')
  } finally {
    importingL1.value = false
    target.value = ''
  }
}

const handleExportL2 = async () => {
  exportingL2.value = true
  try {
    await exportL2Memory()
    notify('L2 记忆导出成功')
  } catch (e: unknown) {
    notify((e as Error).message || '导出失败', 'error')
  } finally {
    exportingL2.value = false
  }
}

const triggerImportL2 = () => {
  l2FileInput.value?.click()
}

const handleImportL2File = async (event: Event) => {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  importingL2.value = true
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    const stats = await importL2Memory(data)
    notify(`L2 记忆导入成功：导入 ${stats.imported_count} 条，跳过 ${stats.skipped_count} 条`)
  } catch (e: unknown) {
    notify((e as Error).message || '导入失败', 'error')
  } finally {
    importingL2.value = false
    target.value = ''
  }
}

const handleExportL3 = async () => {
  exportingL3.value = true
  try {
    await exportL3KG()
    notify('L3 知识图谱导出成功')
  } catch (e: unknown) {
    notify((e as Error).message || '导出失败', 'error')
  } finally {
    exportingL3.value = false
  }
}

const triggerImportL3 = () => {
  l3FileInput.value?.click()
}

const handleImportL3File = async (event: Event) => {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  importingL3.value = true
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    const stats = await importL3KG(data)
    notify(`知识图谱导入成功：${stats.imported_nodes} 节点，${stats.imported_edges} 边`)
  } catch (e: unknown) {
    notify((e as Error).message || '导入失败', 'error')
  } finally {
    importingL3.value = false
    target.value = ''
  }
}

const handleExportProfile = async () => {
  exportingProfile.value = true
  try {
    await exportProfiles()
    notify('画像导出成功')
  } catch (e: unknown) {
    notify((e as Error).message || '导出失败', 'error')
  } finally {
    exportingProfile.value = false
  }
}

const triggerImportProfile = () => {
  profileFileInput.value?.click()
}

const handleImportProfileFile = async (event: Event) => {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  importingProfile.value = true
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    const stats = await importProfiles(data)
    notify(`画像导入成功：${stats.imported_groups} 群聊，${stats.imported_users} 用户`)
  } catch (e: unknown) {
    notify((e as Error).message || '导入失败', 'error')
  } finally {
    importingProfile.value = false
    target.value = ''
  }
}

const handleExportAll = async () => {
  exportingAll.value = true
  try {
    await exportAll()
    notify('全量备份成功')
  } catch (e: unknown) {
    notify((e as Error).message || '备份失败', 'error')
  } finally {
    exportingAll.value = false
  }
}

const triggerImportAll = () => {
  allFileInput.value?.click()
}

const handleImportAllFile = async (event: Event) => {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  showConfirm('确认要恢复全量数据吗？已存在的记录将被跳过。', async () => {
    importingAll.value = true
    try {
      const text = await file.text()
      const data = JSON.parse(text)
      await importAll(data)
      notify('全量恢复完成')
    } catch (e: unknown) {
      notify((e as Error).message || '恢复失败', 'error')
    } finally {
      importingAll.value = false
      target.value = ''
    }
  })
}

const handleClearL1 = () => {
  showConfirm('确认要清空所有 L1 缓冲吗？此操作不可逆。', async () => {
    clearingL1.value = true
    try {
      const result = await clearL1Buffer()
      notify(`L1 缓冲已清空：${result.cleared_count} 条消息`)
    } catch (e: unknown) {
      notify((e as Error).message || '清空失败', 'error')
    } finally {
      clearingL1.value = false
    }
  })
}

const handleDeleteL2 = () => {
  showConfirm('确认要删除所有 L2 记忆吗？此操作不可逆。', async () => {
    deletingL2.value = true
    try {
      const result = await deleteL2Memory('all')
      notify(`L2 记忆已删除：${result.deleted_count} 条`)
    } catch (e: unknown) {
      notify((e as Error).message || '删除失败', 'error')
    } finally {
      deletingL2.value = false
    }
  })
}

const handleDeleteL3 = () => {
  showConfirm('确认要删除所有 L3 知识图谱吗？此操作不可逆。', async () => {
    deletingL3.value = true
    try {
      const result = await deleteL3KG('all')
      notify(`L3 图谱已删除：${result.deleted_count} 节点`)
    } catch (e: unknown) {
      notify((e as Error).message || '删除失败', 'error')
    } finally {
      deletingL3.value = false
    }
  })
}

const handleMergeL3 = async () => {
  mergingL3.value = true
  try {
    const result = await mergeL3Duplicates()
    notify(`合并完成：${result.merged_count} 组，删除 ${result.deleted_count} 个节点`)
  } catch (e: unknown) {
    notify((e as Error).message || '合并失败', 'error')
  } finally {
    mergingL3.value = false
  }
}

const handleDeleteProfile = () => {
  showConfirm('确认要删除所有画像数据吗？此操作不可逆。', async () => {
    deletingProfile.value = true
    try {
      await deleteProfile('all')
      notify('所有画像已删除')
    } catch (e: unknown) {
      notify((e as Error).message || '删除失败', 'error')
    } finally {
      deletingProfile.value = false
    }
  })
}
</script>
