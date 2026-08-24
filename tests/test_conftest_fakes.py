"""Tests for FakeFirestoreClient and related test doubles in conftest.py."""


def test_fake_doc_ref_delete_removes_the_doc(fake_firestore):
    fake_firestore.collection("things").document("a").set({"x": 1})

    fake_firestore.collection("things").document("a").delete()

    doc = fake_firestore.collection("things").document("a").get()
    assert not doc.exists
