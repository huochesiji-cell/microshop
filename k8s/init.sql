CREATE DATABASE IF NOT EXISTS microshop DEFAULT CHARACTER SET utf8mb4;
USE microshop;
CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product VARCHAR(100) NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO orders (product, quantity) VALUES
    ('iPhone 15 Pro', 2),
    ('MacBook Air', 1),
    ('AirPods Pro', 3);
