import asyncio

import httpx
import pytest

from echotrace.util import dumps


async def test_custom_resume_budget_is_validated_and_added_exactly(app):
    state = app.state
    received = []
    async def handler(job_id, payload):
        received.append(payload.copy())
        return {}
    state.jobs.handlers['distill'] = handler
    original = {'token_limit': 500000, 'version': 3, 'consent': True}
    state.db.execute("INSERT INTO jobs(id,kind,status,payload) VALUES('budget','distill','paused',?)", (dumps(original),))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        for value in (-1, 1.5, True, '1000000', None, 100000001):
            response = await client.post('/api/jobs/budget/resume', json={'extra_budget': value})
            assert response.status_code == 422, response.text
            assert state.jobs.get('budget')['payload'] == original
            assert state.jobs.get('budget')['status'] == 'paused'
        response = await client.post('/api/jobs/budget/resume', json={'extra_budget': 3500123})
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(state.jobs.tasks.values()))
        assert received == [{**original, 'token_limit': 4000123}]
        state.db.execute("INSERT INTO jobs(id,kind,status,payload) VALUES('plain','distill','paused',?)", (dumps(original),))
        response = await client.post('/api/jobs/plain/resume', json={})
        assert response.status_code == 200
        await asyncio.gather(*list(state.jobs.tasks.values()))
        assert received[-1] == original
