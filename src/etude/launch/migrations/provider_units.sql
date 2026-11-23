-- after: spot_rentals
ALTER TABLE providers ADD COLUMN usd_per_unit TEXT;
ALTER TABLE rentals ADD COLUMN unit TEXT;
ALTER TABLE rentals ADD COLUMN units_per_hour TEXT;
ALTER TABLE rentals ADD COLUMN usd_per_unit TEXT;
