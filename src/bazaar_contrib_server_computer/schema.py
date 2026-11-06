"""The computers, taken over from the published history in the shape its
last version left them."""

import bazaar_contrib_storage as s
from bazaar_contrib_schema import Dependency, Migration, ModuleSchema

COMPUTERS = s.Table(
    name="computers",
    columns=(
        s.ColumnDefinition(name="id", type="text", nullable=True),
        s.ColumnDefinition(name="name", type="text"),
        s.ColumnDefinition(name="secret_hash", type="text"),
        s.ColumnDefinition(name="created_at_ms", type="integer"),
    ),
    primary_key=("id",),
)

BASELINE = Migration(
    version=1, name="computers", operations=(s.CreateTable(table=COMPUTERS),)
)

MODULE = ModuleSchema(
    tables=("computers",),
    migrations=(BASELINE,),
    baseline=BASELINE,
    predecessor=Dependency(package="bazaar-contrib-server-migrations", version=7),
)
