"""
Unit tests for bot.spawn_background_task.

Added after an adversarial review noted that bot.py's fire-and-forget
asyncio.create_task(...) calls (the enforce_bans_loop restart, and the
background enforcement/sync tasks in enable_cmd/sync_now_cmd) held no
reference to the created Task. Per the asyncio docs, a Task with nothing
else referencing it can be garbage-collected before it finishes -- a real,
if narrow, footgun. spawn_background_task keeps a strong reference in a
module-level set until the task completes, then discards it.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_background_tasks -v
"""
import asyncio
import unittest

import bot


class SpawnBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_a_task_that_actually_runs(self):
        ran = False

        async def _work():
            nonlocal ran
            ran = True

        task = bot.spawn_background_task(_work())
        self.assertIsInstance(task, asyncio.Task)
        await task

        self.assertTrue(ran)

    async def test_task_is_tracked_while_running_and_untracked_once_done(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def _work():
            started.set()
            await release.wait()

        task = bot.spawn_background_task(_work())
        await started.wait()

        self.assertIn(task, bot._background_tasks)

        release.set()
        await task

        self.assertNotIn(task, bot._background_tasks)

    async def test_tracks_multiple_concurrent_tasks_independently(self):
        release = asyncio.Event()

        async def _work():
            await release.wait()

        task_a = bot.spawn_background_task(_work())
        task_b = bot.spawn_background_task(_work())

        self.assertIn(task_a, bot._background_tasks)
        self.assertIn(task_b, bot._background_tasks)

        release.set()
        await asyncio.gather(task_a, task_b)

        self.assertNotIn(task_a, bot._background_tasks)
        self.assertNotIn(task_b, bot._background_tasks)

    async def test_task_that_raises_is_still_untracked_afterward(self):
        async def _work():
            raise RuntimeError("boom")

        task = bot.spawn_background_task(_work())
        with self.assertRaises(RuntimeError):
            await task

        self.assertNotIn(task, bot._background_tasks)


if __name__ == "__main__":
    unittest.main()
