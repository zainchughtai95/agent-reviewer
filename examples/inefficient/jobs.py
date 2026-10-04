"""PySpark anti-patterns that pull work onto the driver or disable Catalyst."""

from pyspark.sql import SparkSession
from pyspark.sql.functions import udf
from pyspark.sql.types import StringType


def run(spark: SparkSession) -> None:
    orders = spark.table("orders")
    customers = spark.table("customers")

    rows = orders.collect()
    pdf = customers.toPandas()

    @udf(StringType())
    def upper_name(value: str) -> str:
        return value.upper() if value else ""

    labeled = customers.withColumn("name_upper", upper_name("name"))

    total = 0
    for _ in range(5):
        total += orders.count()

    huge = orders.crossJoin(customers)
    dumped = huge.coalesce(1)
    dumped.write.mode("overwrite").parquet("/tmp/dump")
    labeled.rdd.map(lambda row: row.name_upper).take(10000)
    print(len(rows), len(pdf), total)
