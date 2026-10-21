SELECT id, name, status, stock, seller_id, created_at, updated_at FROM products;
SELECT id, user_id, status, total_amount, discount_amount, promo_code_id FROM orders;
SELECT order_id, product_id, quantity, price_at_order FROM order_items;
SELECT code, current_uses, max_uses FROM promo_codes;
SELECT user_id, operation_type, created_at FROM user_operations;
SELECT version, description, success FROM flyway_schema_history ORDER BY installed_rank;
