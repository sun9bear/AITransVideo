import { RegisterPageContent } from "@/components/auth/register-page-content"

/**
 * Registration page at /auth — primary entry for "免费开始试用".
 * Phone verification + set password for new users.
 * Existing users who verify phone are auto-logged in.
 */
export default function RegisterPage() {
  return <RegisterPageContent />
}
