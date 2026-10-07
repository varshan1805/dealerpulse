.PHONY: test test-unit test-browser lint sample build deploy data clean

test:            ## run every test (unit + dashboard browser tests)
	python3 -m unittest discover -s tests -v

test-unit:       ## unit tests only, no browser needed
	python3 -m unittest tests.test_core tests.test_handlers -v

test-browser:    ## dashboard tests (needs: pip install playwright && playwright install chromium)
	python3 -m unittest tests.test_dashboard -v

lint:            ## static-check the CloudFormation/SAM template
	cfn-lint template.yaml

sample:          ## run the pipeline locally and refresh dashboard sample data
	python3 scripts/local_pipeline.py --sample-js

data:            ## generate a messy synthetic CSV to upload
	python3 scripts/generate_data.py --rows 800 --dirty --out leads.csv

build:
	sam build

deploy:          ## first time: answer the prompts, say yes to saving samconfig.toml
	sam deploy --guided

clean:
	rm -rf out .aws-sam leads.csv __pycache__ tests/__pycache__ src/__pycache__
