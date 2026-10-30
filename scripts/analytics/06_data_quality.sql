-- Review these checks before interpreting financial totals and age-based cohorts.
-- The 1:1 non-RUB flag requires review, not automatic revaluation.
-- title: 06 Проверка данных
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), paid AS (
    SELECT p.* FROM payments p WHERE p.status = 'succeeded'
), active AS (
    SELECT s.* FROM subscriptions s CROSS JOIN cfg c
    WHERE s.is_active IS TRUE AND s.end_date > c.as_of
), duplicate_active AS (
    SELECT user_id FROM active GROUP BY user_id HAVING COUNT(*) > 1
)
SELECT v.check_name AS "Проверка", v.value AS "Количество"
FROM cfg c
CROSS JOIN LATERAL (VALUES
    (1, 'Успешных платежей без base_amount', (SELECT COUNT(*) FROM paid WHERE base_amount IS NULL)),
    (2, 'Не-RUB платежей с курсом 1:1: проверить миграцию',
        (SELECT COUNT(*) FROM paid WHERE UPPER(currency) <> 'RUB' AND fx_rate = 1)),
    (3, 'Успешных платежей без положительного срока',
        (SELECT COUNT(*) FROM paid WHERE subscription_duration_months IS NULL OR subscription_duration_months <= 0)),
    (4, 'Успешных платежей без даты создания', (SELECT COUNT(*) FROM paid WHERE created_at IS NULL)),
    (5, 'Успешных платежей с будущей датой', (SELECT COUNT(*) FROM paid WHERE created_at > c.as_of)),
    (6, 'Пользователей без даты регистрации', (SELECT COUNT(*) FROM users WHERE registration_date IS NULL)),
    (7, 'Пользователей с будущей регистрацией', (SELECT COUNT(*) FROM users WHERE registration_date > c.as_of)),
    (8, 'Платежей с датой раньше регистрации пользователя',
        (SELECT COUNT(*) FROM paid p JOIN users u ON u.user_id = p.user_id WHERE p.created_at < u.registration_date)),
    (9, 'Успешных платежей с отрицательной суммой',
        (SELECT COUNT(*) FROM paid WHERE amount < 0 OR base_amount < 0)),
    (10, 'Успешных платежей без записанного тарифа устройств',
        (SELECT COUNT(*) FROM paid WHERE hwid_device_limit IS NULL)),
    (11, 'Пользователей с несколькими активными подписками', (SELECT COUNT(*) FROM duplicate_active)),
    (12, 'Активных платных подписок без подтверждённых покупок',
        (SELECT COUNT(*) FROM active s WHERE s.provider IS NOT NULL AND s.duration_months > 0
            AND NOT EXISTS (SELECT 1 FROM paid p WHERE p.user_id = s.user_id AND p.amount > 0
                AND (p.created_at <= c.as_of OR p.created_at IS NULL)))),
    (13, 'Заблокированных с действующей подпиской',
        (SELECT COUNT(DISTINCT s.user_id) FROM active s JOIN users u ON u.user_id = s.user_id WHERE u.is_banned IS TRUE))
) AS v(sort_order, check_name, value)
ORDER BY v.sort_order;
