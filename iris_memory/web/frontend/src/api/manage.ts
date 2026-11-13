import { apiPost } from './request'

interface ApiBaseResponse {
  success: boolean
  error?: string
}

function checkSuccess(response: ApiBaseResponse, errorMsg: string): void {
  if (!response.success) {
    throw new Error(response.error || errorMsg)
  }
}

export async function clearL1Buffer(groupId?: string): Promise<{ cleared_count: number }> {
  const response = await apiPost<any>('manage/l1/clear', {
    group_id: groupId ?? null
  })
  checkSuccess(response, '清空 L1 缓冲失败')
  return { cleared_count: response.cleared_count }
}

export async function deleteL2Memory(scope: 'all' | 'group', groupId?: string): Promise<{ deleted_count: number }> {
  const response = await apiPost<any>('manage/l2/delete', {
    scope,
    group_id: groupId
  })
  checkSuccess(response, '删除 L2 记忆失败')
  return { deleted_count: response.deleted_count }
}

export async function deleteL3KG(scope: 'all' | 'group', groupId?: string): Promise<{ deleted_count: number }> {
  const response = await apiPost<any>('manage/l3/delete', {
    scope,
    group_id: groupId
  })
  checkSuccess(response, '删除 L3 图谱失败')
  return { deleted_count: response.deleted_count }
}

export async function mergeL3Duplicates(): Promise<{ merged_count: number; deleted_count: number }> {
  const response = await apiPost<any>('manage/l3/merge-duplicates')
  checkSuccess(response, '合并重复节点失败')
  return { merged_count: response.merged_count, deleted_count: response.deleted_count }
}

export async function deleteProfile(
  scope: 'group' | 'user' | 'all',
  groupId?: string,
  userId?: string
): Promise<Record<string, unknown>> {
  const response = await apiPost<any>('manage/profile/delete', {
    scope,
    group_id: groupId,
    user_id: userId
  })
  checkSuccess(response, '删除画像失败')
  return response
}
