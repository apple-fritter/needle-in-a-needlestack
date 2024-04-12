import concurrent
import copy
import json
import os
import random

import openai
import tiktoken

from evaluator import DefaultEvaluator
from llm_client import LLM_CLIENT_LIST

NUMBER_OF_TEST_QUESTIONS_TO_GENERATE = 10

GENERATED_QUESTION_FILE = "questions.json"

FULL_QUESTION_FILE = "full_questions.json"

PROMPT_SIZE_LIST = [ 1500, 6000, 12000, 100000]

ROUGH_QUESTION_LOCATIONS = [100, 1200, 5700, 11700, 80000, 99700]

NUMBER_OF_QUESTIONS_PER_PROMPT = 5

INTRO_TO_PROMPT = "This is a test to see how well you are paying attention. This text is a series of limericks. " \
    "At the end of the list of limericks, there will be a question. The question will be about one of the limericks. " \
    "Please answer the question as concisely as possible. "


EVALUATOR_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)
TEST_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)

class Limerick:
    def __init__(self, id, author, text, question=None, answer=None, tokens=None, token_count=None, target_location=0):
        self.id = id
        self.author = author
        self.text = text
        self.question = question
        self.answer = answer
        self.tokens = tokens
        self.token_count = token_count
        self.target_location = target_location

    def generate_tokens(self, encoder):
        self.tokens = encoder.encode(self.text)
        self.token_count = len(self.tokens)

    def to_dict(self):
        result = copy.copy(vars(self))
        result.pop("tokens", None)
        return result

    @staticmethod
    def from_dict(dictionary):
        dictionary.pop("is_limerick", None)
        limerick_text = dictionary.get("limerick", None)
        if limerick_text is not None:
            dictionary.pop("limerick", None)
            dictionary["text"] = limerick_text
        result = Limerick(**dictionary)
        return result

class LimerickPrompt:
    def __init__(self, target_size, question_list, text, token_count=0, limerick_list=None):
        self.target_size = target_size
        self.question_list = question_list
        self.limerick_list = limerick_list
        self.text = text
        self.token_count = token_count

    def add_limerick(self, limerick):
        if self.token_count + limerick.token_count <= self.target_size:
            if self.limerick_list is None:
                self.limerick_list = []
            self.limerick_list.append(limerick)
            self.token_count += limerick.token_count

    def build_text_from_limerick_list(self, question, location, repeat_count_for_questions=1):
        result = None
        if location < self.token_count:
            last_token_count = current_token_count = 0
            result = self.text + "\n\n" # intro of prompt was added in the constructor
            for limerick in self.limerick_list:
                current_token_count += limerick.token_count
                if last_token_count < location <= current_token_count:
                    for i in range(repeat_count_for_questions):
                        result = result + "\n\n" + question.text
                result += "\n\n" + limerick.text
                last_token_count = current_token_count
        return result

    def write_to_file(self, file_path):
        with open(file_path, "w") as file:
            json.dump(self.to_dict(), file, indent=4)

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.question_list is not None:
            index = 0
            for question in self.question_list:
                result["question_list"][index] = question.to_dict()
                index += 1
        if self.limerick_list is not None:
            index = 0
            for limerick in self.limerick_list:
                result["limerick_list"][index] = limerick.to_dict()
                index += 1
        return result

    @staticmethod
    def from_dict(dictionary):
        question_list = dictionary.get("question_list", None)
        if question_list is not None:
            dictionary.pop("question_list", None)
            question_list = [Limerick.from_dict(question_dict) for question_dict in question_list]
            dictionary["question_list"] = question_list
        limerick_list = dictionary.get("limerick_list", None)
        if limerick_list is not None:
            dictionary.pop("limerick_list", None)
            limerick_list = [Limerick.from_dict(limerick_dict) for limerick_dict in limerick_list]
            dictionary["limerick_list"] = limerick_list
        result = LimerickPrompt(**dictionary)
        return result

    @staticmethod
    def for_target_size(target_size, question_list):
        result = LimerickPrompt(target_size, copy.copy(question_list), INTRO_TO_PROMPT)
        return result


class LimerickListBuilder:
    def __init__(self, question_dict):
        self.limerick_list = []
        self.limerick_dict = {}
        self.question_dict = question_dict
        self.current_token_count = 0
        self.prior_token_count = 0

    def test_and_add_limerick(self, limerick):
        if self.limerick_dict.get(limerick.id, None) is None and self.question_dict.get(limerick.id, None) is None:
            self.add_limerick(limerick)

    def add_limerick(self, limerick):
        self.prior_token_count = self.current_token_count
        self.limerick_dict[limerick.id] = limerick
        self.limerick_list.append(limerick)
        self.current_token_count += limerick.token_count


def read_and_init_limericks(file_path):
    result = []
    encoder = tiktoken.encoding_for_model("gpt-4")
    with open(file_path, "r") as file:
        limerick_dict_list = json.load(file)
        for limerick_dict in limerick_dict_list:
            limerick = Limerick.from_dict(limerick_dict)
            limerick.generate_tokens(encoder)
            result.append(limerick)
    return result

def select_limericks_to_answer(limerick_list, number_of_answers):
    selected_limerick_dict = {}
    while len(selected_limerick_dict) < number_of_answers:
        index = random.randint(0, len(limerick_list) - 1)
        limerick = limerick_list[index]
        if selected_limerick_dict.get(limerick.id, None) is None:
            selected_limerick_dict[limerick.id] = copy.copy(limerick)
    result = list(selected_limerick_dict.values())
    return result


def generate_answers(limerick_list, number_of_answers, file_path):
    selected_limerick_list = select_limericks_to_answer(limerick_list, number_of_answers)
    result = []
    for limerick in selected_limerick_list:
        print(limerick.text)
        print("Create a question for the limerick above")
        question = input()
        print("What is the answer to the question?")
        answer = input()
        limerick.question = question
        limerick.answer = answer
    result_dict_list = [limerick.to_dict() for limerick in selected_limerick_list]
    with open(file_path, "w") as file:
        json.dump(result_dict_list, file, indent=4)
    return result


def select_questions_for_prompt(file_path, number_of_questions):
    with open(file_path, "r") as file:
        question_dict_list = json.load(file)
    question_list = [Limerick.from_dict(question_dict) for question_dict in question_dict_list]
    selected_question_dict = {}
    while len(selected_question_dict) < number_of_questions:
        index = random.randint(0, len(question_list) - 1)
        question = question_list[index]
        if selected_question_dict.get(question.id, None) is None:
            question = copy.copy(question)
            selected_question_dict[question.id] = question
    result = list(selected_question_dict.values())
    return result, selected_question_dict


def select_limericks_for_prompt(limerick_list, question_dict, max_token_count):
    builder = LimerickListBuilder(question_dict)
    while builder.current_token_count < max_token_count:
        index = random.randint(0, len(limerick_list) - 1)
        limerick = limerick_list[index]
        builder.test_and_add_limerick(limerick)
    result = builder.limerick_list
    return result


def generate_tests(limerick_list, prompt_size_list):
    selected_question_list, selected_question_dict = select_questions_for_prompt(FULL_QUESTION_FILE,
                                                                                 NUMBER_OF_QUESTIONS_PER_PROMPT)
    max_token_count = prompt_size_list[-1]
    selected_limerick_list = select_limericks_for_prompt(limerick_list, selected_question_dict,
                                                         max_token_count)
    prompt_list = [LimerickPrompt.for_target_size(prompt_size, selected_question_list) for prompt_size in prompt_size_list]
    index = 0
    for limerick in selected_limerick_list:
        index += 1
        if index % 10 == 0:
            print(".")
        for prompt in prompt_list:
            prompt.add_limerick(limerick)
    for prompt in prompt_list:
        prompt.write_to_file("test_" + str(prompt.target_size) + ".json")
    return prompt_list


def test_file_name(size):
    result = "test_" + str(size) + ".json"
    return result


def print_result(prompt, client, question, location, result, score):
    print("---------------------------------")
    print("Client:", client.llm_name)
    print("Prompt Size:", prompt.token_count)
    print("Location:", location)
    print("Limerick:", question.text)
    print("Question:", question.question)
    print("Good Answer:", question.answer)
    print("Generated Answer:", result)
    print("Score:", score)


def write_prompt_text_to_file(prompt_text, prompt_file, client_name, location, question_id):
    file_name = prompt_file + "_" + client_name + "_" + location + "_" + question_id + ".txt"
    file_path = os.path.join("full_prompts", file_name)
    with open(file_path, "w") as file:
        file.write(prompt_text)


def run_tests(test_executor, evaluator_executor, prompt_file_list, client_list, question_location_list, evaluator):
    for client in client_list:
        for prompt_file in prompt_file_list:
            with open(prompt_file, "r") as file:
                prompt_dict = json.load(file)
                prompt = LimerickPrompt.from_dict(prompt_dict)
                if client.in_context_window(prompt.token_count):
                    question_list = prompt.question_list
                    for question in question_list:
                        for location in question_location_list:
                            prompt_text = prompt.build_text_from_limerick_list(question, location, 1)
                            if prompt_text is not None:
                                prompt_text += "\n\n" + question.question
                                result = client.prompt(prompt_text)
                                score = evaluator.evaluate(evaluator_executor,question, result)
                                print_result(prompt, client, question, location, result, score)
                                write_prompt_text_to_file(prompt_text, prompt_file, client.llm_name, str(location), str(question.id))


if __name__ == '__main__':
    print("Enter 1 generate questions and answers for a limerick, 2 to generate tests or 3 to run tests:")
    user_input = input()
    user_input = user_input.strip()
    if user_input == "1":
        generate_answers(read_and_init_limericks("limerick_dataset_oedilf_v3.json"), NUMBER_OF_TEST_QUESTIONS_TO_GENERATE, GENERATED_QUESTION_FILE)
    elif user_input == "2":
        generate_tests(read_and_init_limericks("limerick_dataset_oedilf_v3.json"), PROMPT_SIZE_LIST)
    elif user_input == "3":
        run_tests(["prompt_6000.json"], LLM_CLIENT_LIST, ROUGH_QUESTION_LOCATIONS, DefaultEvaluator())
    else:
        print("Invalid input")
