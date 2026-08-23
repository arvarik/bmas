/**
 * /activity → /tasks?status=running redirect
 *
 * The Live activity page merged into the Tasks workspace. Old bookmarks
 * land on the running-tasks view.
 */
import { redirect } from "next/navigation";

export default function ActivityRedirect() {
  redirect("/tasks?status=running");
}
