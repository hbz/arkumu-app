# arkumu

Archive der Kunst- und Musikhochschulen

This is the repository for [arkumu.nrw](https://www.dh.nrw/kooperationen/arkumu.nrw-108), a consortium project of art and music universities in North Rhine-Westphalia, Germany. The project aims to digitize and make available multimedia content from participating institutions, providing access to artistic-scientific digital archives for research, teaching, and the general public.

[![Built with Cookiecutter Django](https://img.shields.io/badge/built%20with-Cookiecutter%20Django-ff69b4.svg?logo=cookiecutter)](https://github.com/cookiecutter/cookiecutter-django/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Codacy Badge](https://app.codacy.com/project/badge/Grade/e91cb1f481564f2b87503f2aeecfa17b)](https://app.codacy.com/gh/hbz/arkumu-app/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)

License: MIT

## Quick Start with Docker

To get the application running locally:
```
$ docker compose -f docker-compose.local.yml up
```

Create a superuser account:
```
$ docker compose -f docker-compose.local.yml run --rm django python manage.py createsuperuser
```

**You can access the main page at:**
http://localhost:8000

## CSV Column Annotation Guide

This guide explains how to use the different column annotation options when working with CSV data mapping.

### What are Column Annotations?

Column annotations help you describe what each column in your data represents and how it connects to other information. Think of them as labels that help the system understand your data better.

### Types of Annotations

#### 🔗 Foreign Key (FK)
**What it does:** Links your column to data in another table or dataset.

**When to use:** When your column contains IDs or references that point to information stored elsewhere.

**Example:** If you have a "Customer ID" column that refers to customer details in another table.

**Real-world example:** See the "Practical Example: Many-to-Many Relationships" section below for a clear demonstration of how to use Foreign Keys in junction tables.

#### 🔀 Multi-value
**What it does:** Tells the system that one cell might contain multiple pieces of information separated by commas or other characters.

**When to use:** When a single cell contains multiple values that should be treated separately.

**Example:** A "Skills" column containing "Python, JavaScript, SQL" should be split into three separate skills.

#### ⚓ Anchor Column
**What it does:** Marks this column as the main identifier for building relationships between different pieces of data.

**When to use:** Choose your most important identifying column - usually an ID or unique name.

**Example:** A "Person ID" or "Product Code" that uniquely identifies each row.

#### 🏷️ Relationship Context
**What it does:** Describes the connection between two related pieces of information.

**When to use:** When you need to specify how two things are related to each other.

**Example:** A "Role" column that describes how a person relates to an organization (employee, contractor, etc.).

**Real-world example:** See the "Practical Example: Many-to-Many Relationships" section below for a clear demonstration of how to use Relationship Context in junction tables.

#### 🌐 Ontology
**What it does:** Maps your column to standardized property names or vocabularies to ensure consistent data modeling.

**When to use:** When you want to use established property names or connect your data to standard vocabularies like Dublin Core, FOAF, or other ontologies.

**Example:** Mapping a "Name" column to the standard property "foaf:name" or a "Date Created" column to "dc:created".

**Benefits:** 
- Ensures consistent property naming across different datasets
- Makes your data more interoperable with other systems
- Improves data integration and searchability

### Practical Example: Many-to-Many Relationships

Let's look at a generic example of a junction table that represents a many-to-many relationship between persons and projects:

```
record_id;person_id;project_id;role;...
"1";"101";"201";"Author";...
"2";"102";"202";"Designer";...
```

In this table:
1. **person_id** should be annotated as a 🔗 **Foreign Key** pointing to the persons table (ID column)
2. **project_id** should be annotated as a 🔗 **Foreign Key** pointing to the projects table (ID column)
3. **role** should be annotated as a 🏷️ **Relationship Context** as it describes the role/activity that connects the person to the project (e.g., "Author", "Designer", "Editor")
4. **record_id** could be annotated as an ⚓ **Anchor Column** if you need to reference this specific relationship elsewhere

This pattern is common in junction/linking tables that not only connect two entities but also provide additional context about the nature of their relationship.

### Sample Values

Each column shows sample data from your file to help you understand what's in that column. Use these examples to decide which annotations make sense.

## Technical Documentation

- [URL Creation and Import Process](URL_CREATION_AND_IMPORT_PROCESS.md) - Detailed explanation of how URIs are generated and data is imported

## Management Command Notes

- `canonicalize_schema_manifest` normalizes mapping manifests against `metadata_resource` canonical URIs. Tests live in `arkumu/storage/tests/management/test_canonicalize_schema_manifest.py` and can be executed with `docker compose -f docker-compose.local.yml run --rm django pytest arkumu/storage/tests/management/test_canonicalize_schema_manifest.py`.

## Development

For detailed development instructions, testing, deployment, and other advanced features, see the [Cookiecutter Django documentation](https://cookiecutter-django.readthedocs.io/).
