def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_item(client):
    created = client.post("/items", json={"sku": "BOLT-M8", "name": "M8 bolt", "quantity": 120}).json()

    assert client.get(f"/items/{created['id']}").json() == created


def test_update_quantity(client):
    item = client.post("/items", json={"sku": "NUT-M8", "name": "M8 nut", "quantity": 10}).json()

    response = client.patch(f"/items/{item['id']}", json={"quantity": 4})

    assert response.json()["quantity"] == 4


def test_missing_item_returns_404(client):
    assert client.get("/items/999").status_code == 404


def test_delete_item(client):
    item = client.post("/items", json={"sku": "WASH-8", "name": "Washer", "quantity": 50}).json()

    assert client.delete(f"/items/{item['id']}").status_code == 204
    assert client.get(f"/items/{item['id']}").status_code == 404
