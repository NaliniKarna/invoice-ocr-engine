from tests.fakes import png_bytes


def upload(client, tenant, data=None, name="bill.png", ctype="image/png", headers=None, meta=None):
    h = dict(tenant.h, **(headers or {}))
    form = {"metadata": meta} if meta else None
    return client.post("/api/v1/bills", files={"file": (name, data if data is not None else png_bytes(), ctype)},
                       data=form, headers=h)


def submit_and_process(client, tenant, run_queue, data=None):
    r = upload(client, tenant, data)
    assert r.status_code == 202, r.text
    run_queue()
    return r.json()["process_id"]
