import { apiGet, apiPost } from './request'

export interface ReplyWhitelistGroup {
  group_id: string
  state: string
  willingness: string
  initiate_daily_count: number
  initiate_rolling_24h_count?: number
  initiate_hazard_probability?: number
  initiate_next_check_at?: number
  anchor_kind?: string
  anchor_users?: string[]
  anchor_keywords?: string[]
  anchor_reason?: string
  anchor_bot_message?: string
  initiate_pending?: boolean
}

export async function getReplyWhitelist(): Promise<ReplyWhitelistGroup[]> {
  return apiGet<ReplyWhitelistGroup[]>('reply/whitelist/list')
}

export async function enableReplyGroup(groupId: string): Promise<void> {
  await apiPost('reply/whitelist/enable', { group_id: groupId })
}

export async function disableReplyGroup(groupId: string): Promise<void> {
  await apiPost('reply/whitelist/disable', { group_id: groupId })
}

export async function setReplyWillingness(groupId: string, willingness: string): Promise<void> {
  await apiPost('reply/group/set_willingness', { group_id: groupId, willingness })
}

export async function resetReplyGroup(groupId: string): Promise<void> {
  await apiPost('reply/group/reset', { group_id: groupId })
}
