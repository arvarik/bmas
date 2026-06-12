/**
 * /skills → /agents redirect
 *
 * Keeps old bookmarks working after the slug rename.
 */
import { redirect } from "next/navigation";

export default function SkillsRedirect() {
  redirect("/agents");
}
