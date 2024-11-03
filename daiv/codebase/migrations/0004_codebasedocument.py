# Generated by Django 5.1.2 on 2024-11-03 01:28

import uuid

import django.db.models.deletion
from django.db import migrations, models

import django_extensions.db.fields
import pgvector.django.indexes
import pgvector.django.vector


class Migration(migrations.Migration):
    dependencies = [("codebase", "0003_pgvector_extension")]

    operations = [
        migrations.CreateModel(
            name="CodebaseDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "created",
                    django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name="created"),
                ),
                (
                    "modified",
                    django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name="modified"),
                ),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("path", models.CharField(max_length=256)),
                ("page_content", models.TextField()),
                ("page_content_vector", pgvector.django.vector.VectorField(dimensions=1536)),
                ("metadata", models.JSONField(default=dict)),
                (
                    "namespace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="codebase.codebasenamespace",
                    ),
                ),
            ],
            options={
                "indexes": [
                    pgvector.django.indexes.HnswIndex(
                        ef_construction=64,
                        fields=["page_content_vector"],
                        m=16,
                        name="document_hnsw_index",
                        opclasses=["vector_cosine_ops"],
                    )
                ]
            },
        )
    ]
