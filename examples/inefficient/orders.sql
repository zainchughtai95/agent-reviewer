"""Inefficient SQL a reviewer should catch."""

SELECT *
FROM orders o
CROSS JOIN customers c
WHERE LOWER(o.status) = 'open'
  AND o.customer_id NOT IN (SELECT id FROM blocked_customers)
  AND c.email LIKE '%gmail.com'
ORDER BY o.created_at;
