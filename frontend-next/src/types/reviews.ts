import type { JobSummary } from '@/types/jobs'

export type NativeReviewStage =
  | 'speaker_review'
  | 'translation_review'
  | 'voice_review'
  | 'voice_selection_review'

export interface ReviewSpeakerOption {
  id: string
  displayName: string
}

export interface SpeakerReviewItem {
  segmentId: string
  speakerId: string
  displayName: string
  sourceText: string
  transcriptText: string
  speakerConfirmed: boolean
  transcriptConfirmed: boolean
  reviewUpdatedAt: string | null
}

export interface TranslationReviewItem {
  segmentId: string
  speakerId: string
  displayName: string
  sourceText: string
  cnText: string
  translationConfirmed: boolean
  rewriteRequested: boolean
  reviewUpdatedAt: string | null
  startMs: number
  endMs: number
}

export interface ReviewJobTransition {
  job: JobSummary
}

export interface SpeakerReviewResource {
  activeMessage: string | null
  defaultPageSize: number
  fallbackHref: string
  items: SpeakerReviewItem[]
  job: JobSummary
  pageSizeOptions: number[]
  projectDir: string
  speakerOptions: ReviewSpeakerOption[]
}

export interface TranslationReviewResource {
  activeMessage: string | null
  defaultPageSize: number
  fallbackHref: string
  items: TranslationReviewItem[]
  job: JobSummary
  pageSizeOptions: number[]
  projectDir: string
  speakerOptions: ReviewSpeakerOption[]
}

export interface VoiceReviewAvailableVoice {
  voiceId: string
  voiceType: string | null
  provider: string | null
  ttsProvider: string | null
  platform: string | null
  label: string | null
  createdAt: string | null
  sourceAudioPath: string | null
  notes: string | null
  verificationStatus: string | null
  lastVerifiedAt: string | null
  lastVerificationSuccess: boolean | null
  lastVerificationAudioPath: string | null
  lastVerificationError: string | null
}

export interface VoiceReviewSpeaker {
  speakerId: string
  speakerLabel: string | null
  speakerName: string
  voiceArgName: string | null
  samplePath: string | null
  sampleDurationS: number
  silenceRatio: number
  defaultVoiceId: string | null
  defaultVoiceType: string | null
  resolvedStatus: string | null
  resolvedSource: string | null
  resolvedVoiceId: string | null
  resolvedVoiceType: string | null
  resolvedLabel: string | null
  availableVoices: VoiceReviewAvailableVoice[]
}

export interface VoiceReviewResource {
  activeMessage: string | null
  fallbackHref: string
  job: JobSummary
  projectDir: string
  reason: string | null
  speakers: VoiceReviewSpeaker[]
}

export interface SpeakerReviewApprovalInput {
  confirmations: Record<
    string,
    {
      speakerConfirmed: boolean
      transcriptConfirmed: boolean
      updatedAt: string
    }
  >
  jobId: string
  projectDir: string
  segmentSpeakers: Record<string, string>
  speakerNames: Record<string, string>
}

export interface TranslationReviewApprovalInput {
  jobId: string
  projectDir: string
  segmentSpeakers?: Record<string, string>
  speakerNames?: Record<string, string>
  segments: Record<
    string,
    {
      cnText: string
      rewriteRequested: boolean
      translationConfirmed: boolean
      updatedAt: string
    }
  >
}

export interface VoiceReviewDefaultBindingInput {
  jobId: string
  speakerId: string
  voiceId: string
}

export interface VoiceReviewManualBindingInput {
  jobId: string
  samplePath?: string | null
  speakerId: string
  speakerName: string
  voiceId: string
}

export interface VoiceReviewApprovalInput {
  jobId: string
  projectDir: string
  voiceIdA: string | null
  voiceIdB: string | null
}

export interface VoiceSelectionSpeakerSegment {
  segmentId: number
  startMs: number
  endMs: number
  durationS: number
  sourceText: string
  audioUrl: string
}

export interface VoiceSelectionAutoMatchedVoice {
  voiceId: string
  label: string
  matchConfidence: string
}

export interface VoiceSelectionSpeaker {
  speakerId: string
  speakerName: string
  segmentCount: number
  totalDurationS: number
  autoMatchedVoice: VoiceSelectionAutoMatchedVoice | null
  canClone: boolean
  segments: VoiceSelectionSpeakerSegment[]
}

export interface VoiceSelectionAvailableVoice {
  voiceId: string
  label: string
  gender: string
  provider: string
}

export interface VoiceSelectionPayload {
  message: string
  ttsProvider: string
  speakers: VoiceSelectionSpeaker[]
  availableVoices: VoiceSelectionAvailableVoice[]
  cloneCostCredits: number
}

export interface VoiceSelectionApprovalInput {
  jobId: string
  speakers: Array<{
    speakerId: string
    voiceId: string
    voiceSource: 'catalog' | 'cloned' | 'auto_matched'
  }>
}

export interface VolcEngineStudioVoiceOption {
  voiceId: string
  displayName: string
  gender: string
  ageGroup: string
  personaStyle: string
}
