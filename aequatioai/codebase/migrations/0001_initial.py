# Generated by Django 5.0.6 on 2024-06-04 16:22

import django.db.models.deletion
import django_extensions.db.fields
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='RepositoryInfo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name='created')),
                ('modified', django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name='modified')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('external_slug', models.CharField(max_length=256)),
                ('external_id', models.CharField(max_length=256)),
                ('client', models.CharField(choices=[('gitlab', 'GitLab'), ('github', 'GitHub')], max_length=16)),
            ],
            options={
                'get_latest_by': 'modified',
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='CodebaseNamespace',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name='created')),
                ('modified', django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name='modified')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('sha', models.CharField(max_length=64)),
                ('tracking_branch', models.CharField(blank=True, max_length=256)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('indexing', 'Indexing'), ('indexed', 'Indexed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('repository_info', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='namespaces', to='codebase.repositoryinfo')),
            ],
            options={
                'get_latest_by': 'created',
            },
        ),
    ]
