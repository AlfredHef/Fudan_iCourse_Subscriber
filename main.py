"""iCourse Subscriber — main orchestration.

Runs a single check: login → detect new lectures → stream audio → transcribe
→ summarize → email. Designed to be triggered by GitHub Actions cron.

Lectures are processed concurrently (controlled by MAX_WORKERS).
"""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from src import config
from src.database import Database
from src.emailer import Emailer
from src.icourse import ICourseClient
from src.summarizer import Summarizer
from src.transcriber import IncompleteAudioError, NoAudioStreamError, Transcriber
from src.webvpn import WebVPNSession


class _SessionManager:
    """Thread-safe wrapper around ``ICourseClient``.

    * ``ensure_alive()`` uses double-checked locking so that only one thread
      performs re-login when the WebVPN session expires.
    * After a successful re-login every thread that calls ``ensure_alive()``
      will transparently receive the new client.
    """

    def __init__(self, client: ICourseClient):
        self._client = client
        self._lock = threading.Lock()

    @property
    def client(self) -> ICourseClient:
        """Current client instance (may be stale — prefer ``ensure_alive()``)."""
        return self._client

    def ensure_alive(self) -> ICourseClient:
        """Return a live client, re-logging in if the session has expired.

        Fast path (no lock): if the current client is alive, return it
        immediately.  Slow path: acquire the lock, re-check (another thread
        may have already refreshed), and re-login if still necessary.
        """
        client = self._client
        if client.check_alive():
            return client
        with self._lock:
            # Another thread may have refreshed while we were waiting.
            if self._client.check_alive():
                return self._client
            print("[Session] WebVPN session expired, re-logging in...")
            vpn = login_with_retry()
            self._client = ICourseClient(vpn)
            return self._client


def process_lecture(
    session: _SessionManager,
    db: Database,
    transcriber: Transcriber,
    summarizer: Summarizer,
    course_id: str,
    course_title: str,
    lecture: dict,
) -> str | None:
    """Download, transcribe, and summarize a single lecture.

    Supports stage-skipping: if a previous run already produced a transcript
    or summary, that stage is not repeated.

    Returns the summary string, or None if no summary was produced.
    """
    sub_id = str(lecture["sub_id"])
    sub_title = lecture.get("sub_title", sub_id)
    date = lecture.get("date", "")

    # Tag prefixed to every log line so concurrent output stays readable.
    tag = f"[{sub_title}]"

    print(f"\n  {tag} Processing started ({date})")
    print(f"  {tag} Start: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    t_start = time.time()

    # Check existing progress for stage-skipping
    existing = db.get_lecture(sub_id)
    has_transcript = existing and existing.get("transcript")
    has_summary = existing and existing.get("summary")

    # 1) Transcribe (stream audio directly from CDN — no video download)
    if has_transcript:
        print(f"  {tag} Transcript exists ({len(existing['transcript'])} chars), skipping transcription.")
        transcript = existing["transcript"]
    else:
        print(f"  {tag} Fetching video URL at {time.strftime('%H:%M:%S')}")
        client = session.ensure_alive()
        video_url = client.get_video_url(course_id, sub_id)
        if not video_url:
            print(f"  {tag} No video URL, skipping.")
            return None

        vpn_url, http_headers = client.get_stream_params(video_url)
        print(f"  {tag} Streaming audio at {time.strftime('%H:%M:%S')}")
        print(f"  {tag} URL: {vpn_url[:100]}...")

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                transcript = transcriber.transcribe_url(
                    vpn_url, http_headers=http_headers,
                    label=sub_title,
                )
                db.update_transcript(sub_id, transcript)
                break
            except IncompleteAudioError as e:
                print(f"  {tag} [WARN] Attempt {attempt}/{max_attempts}: {e}")
                if attempt < max_attempts:
                    # Re-login and get fresh URL for retry
                    client = session.ensure_alive()
                    video_url = client.get_video_url(course_id, sub_id)
                    vpn_url, http_headers = client.get_stream_params(video_url)
                    print(f"  {tag} Retrying with fresh connection...")
                else:
                    print(f"  {tag} [FAIL] All {max_attempts} attempts got incomplete audio, using best result.")
                    # Use the partial transcript rather than failing entirely
                    transcript = transcriber.last_transcript
                    db.update_transcript(sub_id, transcript)
            except NoAudioStreamError as e:
                print(f"  {tag} [SKIP] Video-only (no audio stream): {e}")
                db.update_error(sub_id, "transcribe", str(e))
                db.mark_processed(sub_id)
                return None
            except Exception as e:
                print(f"  {tag} [FAIL] Transcription error: {type(e).__name__}: {e}")
                db.update_error(sub_id, "transcribe", str(e))
                raise

    # 2) Summarize
    if not transcript.strip():
        print(f"  {tag} Empty transcript, skipping summary.")
        db.mark_processed(sub_id)
        db.clear_error(sub_id)
        return None

    if has_summary:
        print(f"  {tag} Summary exists ({len(existing['summary'])} chars), skipping summarization.")
        summary = existing["summary"]
    else:
        try:
            print(f"  {tag} Generating summary at {time.strftime('%H:%M:%S')}")
            print(f"  {tag} Transcript length: {len(transcript)} chars")
            summary, model_used = summarizer.summarize(course_title, transcript)
            print(f"  {tag} [OK] Summary by {model_used}: {len(summary)} chars")
            db.update_summary_with_model(sub_id, summary, model_used)
        except Exception as e:
            print(f"  {tag} [FAIL] Summarization error: {type(e).__name__}: {e}")
            db.update_error(sub_id, "summarize", str(e))
            raise

    db.mark_processed(sub_id)
    db.clear_error(sub_id)
    elapsed = time.time() - t_start
    print(f"  {tag} Done at {time.strftime('%H:%M:%S')} (total {elapsed:.0f}s)")
    return summary


def login_with_retry(max_attempts: int = 5) -> WebVPNSession:
    """Login to WebVPN + iCourse CAS with retry (new session each attempt)."""
    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  Failed: {type(e).__name__}, retrying...")
                time.sleep(3)
            else:
                raise


def run():
    """Single execution of the full pipeline.

    Phase 1 – **collect**: iterate every monitored course, identify new or
    previously-failed lectures, and insert them into the database.

    Phase 2 – **process**: feed the collected lectures into a
    ``ThreadPoolExecutor`` so that transcription, summarisation, and
    CDN-signed downloads happen concurrently.  Session expiry is handled
    transparently by ``_SessionManager.ensure_alive()``.
    """
    print("=" * 60)
    print("iCourse Subscriber — starting run")
    print("=" * 60)

    if not config.COURSE_IDS:
        print("No COURSE_IDS configured. Set the COURSE_IDS env var.")
        return

    db = Database()
    transcriber = Transcriber()
    summarizer = Summarizer()
    emailer = Emailer() if config.SMTP_EMAIL and config.SMTP_PASSWORD else None

    vpn = login_with_retry()
    client = ICourseClient(vpn)
    session = _SessionManager(client)

    # ── Phase 1: Collect all pending lectures across all courses ─────────
    all_tasks: list[dict] = []

    for course_id in config.COURSE_IDS:
        try:
            print(f"\n{'─' * 50}")
            print(f"[Course] {course_id}")

            session.ensure_alive()
            detail = session.client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            print(f"  Title: {course_title} (Teacher: {teacher})")
            print(f"  Total lectures: {len(lectures)} ({playback_count} with playback)")

            db.upsert_course(course_id, course_title, teacher)

            # Find new lectures with playback + previously failed (unprocessed) ones
            known_processed = db.get_processed_sub_ids(course_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in known_processed
            ]
            # Deduplicate by sub_title (school system sometimes lists duplicates)
            seen_titles: set[str] = set()
            deduped: list[dict] = []
            for lec in new_lectures:
                title = lec.get("sub_title", "")
                if title in seen_titles:
                    print(f"  [Dedup] Skipping duplicate: {title}"
                          f" (sub_id={lec['sub_id']})")
                    continue
                seen_titles.add(title)
                deduped.append(lec)
            new_lectures = deduped
            # Also retry any previously inserted but unprocessed
            unprocessed = db.get_unprocessed_lectures(course_id)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            # Merge: new from API + retries from DB
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"], "date": u["date"]}
                for u in unprocessed if u["sub_id"] not in new_ids
            ]
            new_lectures.extend(retry_only)

            print(f"  New/retry lectures: {len(new_lectures)}")

            if not new_lectures:
                print("  No new lectures, skipping.")
                continue

            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                all_tasks.append({
                    "course_id": course_id,
                    "course_title": course_title,
                    "lecture": lecture,
                })

        except Exception:
            print(f"  ERROR processing course {course_id}:")
            traceback.print_exc()

    # ── Phase 2: Process lectures concurrently ───────────────────────────
    email_items: list[dict] = []

    if all_tasks:
        max_workers = min(config.MAX_WORKERS, len(all_tasks))
        print(f"\n{'=' * 60}")
        print(f"[Concurrent] Processing {len(all_tasks)} lecture(s)"
              f" with {max_workers} worker(s)")
        print(f"{'=' * 60}")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    process_lecture,
                    session, db, transcriber, summarizer,
                    task["course_id"], task["course_title"], task["lecture"],
                ): task
                for task in all_tasks
            }
            for future in as_completed(futures):
                task = futures[future]
                lecture = task["lecture"]
                sub_id = str(lecture["sub_id"])
                try:
                    summary = future.result()
                    if summary:
                        email_items.append({
                            "sub_id": sub_id,
                            "course_title": task["course_title"],
                            "sub_title": lecture.get("sub_title", sub_id),
                            "date": lecture.get("date", ""),
                            "summary": summary,
                        })
                except Exception:
                    print(f"    ERROR processing {sub_id}:")
                    traceback.print_exc()
    else:
        print("\nNo new lectures to process.")

    # Recover any previously processed-but-unsent lectures
    unsent = db.get_unsent_lectures()
    if unsent:
        seen_sub_ids = {item["sub_id"] for item in email_items}
        for row in unsent:
            if row["sub_id"] not in seen_sub_ids:
                email_items.append({
                    "sub_id": row["sub_id"],
                    "course_title": row["course_title"],
                    "sub_title": row["sub_title"],
                    "date": row["date"],
                    "summary": row["summary"],
                })
        print(f"[Email] Including {len(unsent)} previously unsent lecture(s).")

    # Sort by course then date for correct email grouping and chronological order
    email_items.sort(key=lambda x: (x.get("course_title", ""), x.get("date", "")))

    # Send one email with all summaries
    if emailer and email_items:
        try:
            print(f"\n[Email] Sending summary for {len(email_items)} lecture(s)...")
            if emailer.send(email_items):
                db.mark_emailed_batch([item["sub_id"] for item in email_items])
            else:
                print("[Email] Send failed, lectures will be retried next run.")
        except Exception:
            print("[Email] Failed to send:")
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print("Run complete.")


if __name__ == "__main__":
    run()
